"""Owner sign-in, rate limits, and keeping personal data out of logs."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.security import auth
from app.security.ratelimit import RateLimiter
from app.security.redact import AccessLogFilter, scrub
from app.settings import get_settings

PASS = "plumb-2026"


@pytest.fixture
def locked(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setattr(get_settings(), "owner_passcode", PASS)
    client.cookies.clear()
    yield client


def _sign_in(c: TestClient, passcode: str = PASS) -> int:
    return c.post("/login", data={"passcode": passcode}, follow_redirects=False).status_code


# ── sign-in ─────────────────────────────────────────────────────────────────
def test_open_when_no_passcode_is_set(client: TestClient) -> None:
    assert client.get("/api/home").status_code == 200
    assert client.get("/login", follow_redirects=False).status_code == 303  # nothing to sign into


def test_the_app_and_its_api_need_the_passcode(locked: TestClient) -> None:
    page = locked.get("/", follow_redirects=False)
    assert page.status_code == 303 and page.headers["location"] == "/login"
    for path in ("/api/home", "/api/workspace", "/api/policy", "/api/jobs", "/api/tts/status"):
        assert locked.get(path).status_code == 401, path
    assert locked.post("/api/parse", data={"text": "boiler"}).status_code == 401
    assert locked.put("/api/workspace", json={}).status_code == 401
    assert locked.get("/api-docs", follow_redirects=False).status_code == 303
    assert locked.get("/openapi.json").status_code == 401


def test_client_pages_and_paypal_stay_public(locked: TestClient) -> None:
    assert locked.get("/q/not-a-real-token").status_code == 404  # 404, not "sign in"
    assert locked.get("/p/not-a-real-token").status_code == 404
    assert locked.get("/healthz").status_code == 200
    assert locked.get("/manifest.webmanifest").status_code == 200
    assert locked.get("/sw.js").status_code == 200
    assert locked.get("/static/app.css").status_code == 200
    # Reaches signature verification (400), rather than being bounced for a cookie.
    hook = locked.post(
        "/api/webhooks/paypal", content=b"{}", headers={"content-type": "application/json"}
    )
    assert hook.status_code != 401


def test_wrong_passcode(locked: TestClient) -> None:
    resp = locked.post("/login", data={"passcode": "guess"})
    assert resp.status_code == 401 and "passcode isn" in resp.text
    assert auth.COOKIE not in locked.cookies
    assert locked.get("/api/home").status_code == 401


def test_right_passcode_signs_in(locked: TestClient) -> None:
    resp = locked.post("/login", data={"passcode": f"  {PASS} "}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/"
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age=2592000" in cookie
    assert locked.get("/api/home").status_code == 200
    assert "Sign out of this device" in locked.get("/").text


def test_sign_out(locked: TestClient) -> None:
    _sign_in(locked)
    assert locked.post("/logout", follow_redirects=False).headers["location"] == "/login"
    assert locked.get("/api/home").status_code == 401


def test_forged_old_or_stale_cookies_are_refused(locked: TestClient) -> None:
    s = get_settings()
    good = auth.issue(s)
    assert auth.valid(s, good)
    issued, sig = good.split(".")
    for bad in (
        "", "nonsense", f"{issued}.{'0' * 64}", f"{int(issued) + 1}.{sig}",
        auth.issue(s, now=time.time() - auth.MAX_AGE - 60),  # older than 30 days
        auth.issue(s, now=time.time() + 3600),  # from the future
    ):  # fmt: skip
        locked.cookies.set(auth.COOKIE, bad)
        assert locked.get("/api/home").status_code == 401, bad


def test_changing_the_passcode_signs_everyone_out(
    locked: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _sign_in(locked)
    assert locked.get("/api/home").status_code == 200
    monkeypatch.setattr(get_settings(), "owner_passcode", "new-passcode")
    assert locked.get("/api/home").status_code == 401


def test_cross_site_writes_are_refused(client: TestClient) -> None:
    evil = {"origin": "https://evil.example"}
    assert client.put("/api/workspace", json={}, headers=evil).status_code == 403
    assert client.post("/api/parse", data={"text": "x"}, headers=evil).status_code == 403
    # Same-site is fine (422: the body is empty, but it got through).
    ok = client.put("/api/workspace", json={}, headers={"origin": "http://testserver"})
    assert ok.status_code == 422
    # The client's own pages are their own site; PayPal posts webhooks with no Origin.
    assert client.post("/q/x/messages", json={"text": "hi"}, headers=evil).status_code != 403


def test_login_is_rate_limited(locked: TestClient) -> None:
    for _ in range(5):
        assert _sign_in(locked, "guess") == 401
    resp = locked.post("/login", data={"passcode": PASS}, follow_redirects=False)
    assert resp.status_code == 429 and int(resp.headers["retry-after"]) > 0  # even the right one


def test_production_will_not_start_without_a_passcode(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.main import app

    monkeypatch.setattr(get_settings(), "env", "production")
    monkeypatch.setattr(get_settings(), "owner_passcode", "")
    with pytest.raises(RuntimeError, match="SI_OWNER_PASSCODE"), TestClient(app):
        pass


# ── rate limits ─────────────────────────────────────────────────────────────
def test_sliding_window() -> None:
    rl = RateLimiter()
    assert [rl.hit("k", 3, 60, now=t) for t in (0, 1, 2)] == [0, 0, 0]
    assert rl.hit("k", 3, 60, now=3) == pytest.approx(57)
    assert rl.hit("k", 3, 60, now=60.5) == 0  # the first hit has left the window
    assert rl.hit("other", 3, 60, now=3) == 0  # per key


def test_parse_is_rate_limited(client: TestClient) -> None:
    from app.agents.parser import ParserNotConfigured, get_job_parser
    from app.main import app

    def none() -> None:
        raise ParserNotConfigured("off")

    app.dependency_overrides[get_job_parser] = none
    try:
        codes = [client.post("/api/parse", data={"text": "boiler"}).status_code for _ in range(31)]
    finally:
        app.dependency_overrides.pop(get_job_parser, None)
    assert codes[:30] == [503] * 30 and codes[30] == 429


def test_client_chat_is_rate_limited(client: TestClient) -> None:
    codes = [client.post("/q/nope/messages", json={"text": "hi"}).status_code for _ in range(11)]
    assert 429 not in codes[:10] and codes[10] == 429


# ── no personal data in logs ────────────────────────────────────────────────
def test_scrub() -> None:
    line = scrub("PayPal 422: recipient kennedy@example.com invalid; call +44 7700 900123")
    assert "kennedy@example.com" not in line and "7700" not in line
    assert "[email]" in line and "[number]" in line
    # Ids, amounts and dates survive.
    keep = "invoice 550e8400-e29b-41d4-a716-446655440000 total 1250.00 on 2026-11-05 INV2-Z56S"
    assert scrub(keep) == keep


def test_access_log_drops_query_strings_and_link_tokens() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/api/tts?text=Invoice+for+Mr+Kennedy&lang=en-GB", "1.1", 200), None,
    )  # fmt: skip
    AccessLogFilter().filter(record)
    assert "Kennedy" not in record.getMessage() and "/api/tts?[…]" in record.getMessage()
    record.args = ("1.2.3.4:5", "GET", "/q/Abc123_xyz-LONGTOKENvalue/status", "1.1", 200)
    AccessLogFilter().filter(record)
    assert "LONGTOKEN" not in record.getMessage() and "/q/[token]/status" in record.getMessage()


def test_app_logs_are_scrubbed(capsys: pytest.CaptureFixture[str]) -> None:
    from app.logging_config import configure_logging, get_logger

    configure_logging("development")
    get_logger("t").warning("paypal_send_failed", error="Recipient jo@example.org is not valid")
    out = capsys.readouterr().out
    assert "jo@example.org" not in out and "[email]" in out
