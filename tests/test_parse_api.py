from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from google.genai import errors, types

from app.agents.parser import JobParser, ParserNotConfigured, get_job_parser
from app.main import app
from app.schemas.job import ParsedItem, ParsedJob

KENNEDY = ParsedJob(
    transcript="Fixed Mr Kennedy's boiler, 2 hours at £60, plus £25 for a valve",
    client_name="Mr Kennedy",
    currency="GBP",
    items=[
        ParsedItem(name="Boiler repair", quantity=2, unit_price=60, unit="hours"),
        ParsedItem(name="Valve", quantity=1, unit_price=25, is_material=True),
    ],
    missing_fields=["client_email"],
    follow_up_question="What's Mr Kennedy's email?",
)


class FakeModel:
    """Stands in for Gemini: replays canned drafts or errors, records what it was sent."""

    def __init__(self, *outcomes: ParsedJob | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[list[types.Part], str]] = []

    async def __call__(self, parts: list[types.Part], system: str) -> ParsedJob:
        self.calls.append((parts, system))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome.model_copy(deep=True)


@pytest.fixture
def model() -> Iterator[FakeModel]:
    fake = FakeModel()
    app.dependency_overrides[get_job_parser] = lambda: JobParser(fake)
    yield fake
    app.dependency_overrides.clear()


def _texts(parts: list[types.Part]) -> list[str]:
    return [p.text for p in parts if p.text]


def test_text_parse_returns_a_card_with_code_totals(model: FakeModel) -> None:
    model.outcomes = [KENNEDY]
    with TestClient(app) as client:
        resp = client.post("/api/parse", data={"text": KENNEDY.transcript, "tz": "Europe/London"})
    assert resp.status_code == 200
    card = resp.json()
    assert card["subtotal"] == "145.00"
    assert card["items"][0]["line_total"] == "120.00"
    assert card["ready"] is False
    assert card["missing_fields"] == ["client_email"]
    assert card["follow_up_question"] == "What's Mr Kennedy's email?"
    parts, system = model.calls[0]
    assert _texts(parts) == [KENNEDY.transcript]
    assert "workspace currency is GBP" in system


def test_audio_goes_to_the_model_with_a_clean_mime(model: FakeModel) -> None:
    model.outcomes = [KENNEDY]
    with TestClient(app) as client:
        resp = client.post(
            "/api/parse",
            files={"audio": ("note.webm", b"\x1aE\xdf\xa3fake", "audio/webm;codecs=opus")},
        )
    assert resp.status_code == 200
    parts, _ = model.calls[0]
    assert parts[0].inline_data is not None
    assert parts[0].inline_data.mime_type == "audio/webm"
    assert parts[0].inline_data.data == b"\x1aE\xdf\xa3fake"


def test_follow_up_answer_completes_the_card(model: FakeModel) -> None:
    answered = KENNEDY.model_copy(
        update={
            "transcript": "kennedy@example.com",
            "client_email": "kennedy@example.com",
            "missing_fields": [],
            "follow_up_question": "",
        }
    )
    model.outcomes = [KENNEDY, answered]
    with TestClient(app) as client:
        first = client.post("/api/parse", data={"text": KENNEDY.transcript}).json()
        resp = client.post(
            "/api/parse",
            data={"text": "kennedy@example.com", "previous": json.dumps(first["draft"])},
        )
    card = resp.json()
    assert card["ready"] is True
    assert card["client_email"] == "kennedy@example.com"
    # The stored transcript keeps the whole exchange.
    assert card["transcript"] == f"{KENNEDY.transcript}\nkennedy@example.com"
    parts, _ = model.calls[1]
    texts = _texts(parts)
    assert "Current draft:" in texts[0] and "Mr Kennedy" in texts[0]
    assert texts[1] == "kennedy@example.com"


def test_requested_quote_mode_wins(model: FakeModel) -> None:
    model.outcomes = [KENNEDY]
    with TestClient(app) as client:
        card = client.post("/api/parse", data={"text": "quote it", "mode": "quote"}).json()
    assert card["mode"] == "quote"
    assert card["ready"] is True  # a name is enough for a quote


def test_one_retry_then_success(model: FakeModel) -> None:
    model.outcomes = [TimeoutError(), KENNEDY]
    with TestClient(app) as client:
        resp = client.post("/api/parse", data={"text": "boiler"})
    assert resp.status_code == 200
    assert len(model.calls) == 2


def test_two_failures_offer_type_instead(model: FakeModel) -> None:
    model.outcomes = [TimeoutError(), errors.ServerError(503, {"error": {"message": "busy"}})]
    with TestClient(app) as client:
        resp = client.post("/api/parse", data={"text": "boiler"})
    assert resp.status_code == 503
    assert "type it instead" in resp.json()["detail"]


def test_bad_request_to_the_model_is_not_retried(model: FakeModel) -> None:
    model.outcomes = [errors.ClientError(400, {"error": {"message": "bad audio"}}), KENNEDY]
    with TestClient(app) as client:
        resp = client.post("/api/parse", data={"text": "boiler"})
    assert resp.status_code == 503
    assert len(model.calls) == 1


@pytest.mark.parametrize(
    ("kwargs", "status"),
    [
        ({"data": {}}, 422),
        ({"data": {"text": "   "}}, 422),
        ({"data": {"text": "x" * 2001}}, 413),
        ({"files": {"audio": ("a.png", b"\x89PNG", "image/png")}}, 415),
        ({"data": {"text": "hi", "previous": "{not json"}}, 422),
        ({"data": {"previous": KENNEDY.model_dump_json()}}, 422),  # a draft but no answer
    ],
)
def test_bad_input_is_refused_before_the_model(
    model: FakeModel, kwargs: dict[str, object], status: int
) -> None:
    with TestClient(app) as client:
        resp = client.post("/api/parse", **kwargs)  # type: ignore[arg-type]
    assert resp.status_code == status
    assert model.calls == []


def test_oversized_audio_is_refused(model: FakeModel, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.settings import get_settings

    monkeypatch.setattr(get_settings(), "max_audio_bytes", 10)
    with TestClient(app) as client:
        resp = client.post("/api/parse", files={"audio": ("a.webm", b"x" * 11, "audio/webm")})
    assert resp.status_code == 413
    assert model.calls == []


def test_no_gemini_key_is_503_not_500() -> None:
    def not_configured() -> None:
        raise ParserNotConfigured("no key")

    app.dependency_overrides[get_job_parser] = not_configured
    try:
        with TestClient(app) as client:
            resp = client.post("/api/parse", data={"text": "boiler"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 503
