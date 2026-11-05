"""First-run setup (onboarding), the business settings, and the installable-app files."""

from __future__ import annotations

from fastapi.testclient import TestClient

GOOD = {
    "business_name": "Dave's Plumbing",
    "owner_name": "Dave",
    "currency": "EUR",
    "default_due_days": 14,
}


def test_a_new_workspace_starts_onboarding(client: TestClient) -> None:
    ws = client.get("/api/workspace").json()
    assert ws["onboarded"] is False
    assert "GBP" in ws["currencies"] and "EUR" in ws["currencies"]
    assert ws["paypal_env"] == "sandbox"
    assert "onboarded: false" in client.get("/").text


def test_saving_the_business_finishes_onboarding(client: TestClient) -> None:
    resp = client.put("/api/workspace", json=GOOD)
    assert resp.status_code == 200
    ws = resp.json()
    assert ws["business_name"] == "Dave's Plumbing"
    assert ws["owner_name"] == "Dave"
    assert ws["currency"] == "EUR"
    assert ws["default_due_days"] == 14
    assert ws["onboarded"] is True
    page = client.get("/").text
    assert "onboarded: true" in page
    assert "Dave&#39;s Plumbing" in page or "Dave's Plumbing" in page


def test_new_jobs_use_the_chosen_currency(client: TestClient) -> None:
    client.put("/api/workspace", json=GOOD)
    assert 'currency: "EUR"' in client.get("/").text


def test_editing_later_never_un_onboards(client: TestClient) -> None:
    client.put("/api/workspace", json=GOOD)
    resp = client.put(
        "/api/workspace", json={**GOOD, "business_name": "Dave & Sons", "onboarded": False}
    )
    assert resp.json()["onboarded"] is True
    assert resp.json()["business_name"] == "Dave & Sons"


def test_whitespace_is_trimmed(client: TestClient) -> None:
    ws = client.put("/api/workspace", json={**GOOD, "business_name": "  Dave's Plumbing  "}).json()
    assert ws["business_name"] == "Dave's Plumbing"


def test_bad_details_are_refused(client: TestClient) -> None:
    for bad in (
        {"business_name": ""},
        {"business_name": "   "},
        {"business_name": "x" * 121},
        {"currency": "XYZ"},
        {"currency": "gbp!"},
        {"default_due_days": -1},
        {"default_due_days": 91},
    ):
        assert client.put("/api/workspace", json={**GOOD, **bad}).status_code == 422, bad
    assert client.get("/api/workspace").json()["onboarded"] is False


def test_manifest_makes_it_installable(client: TestClient) -> None:
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/manifest+json")
    m = resp.json()
    assert m["display"] == "standalone" and m["start_url"] == "/"
    sizes = {i["sizes"] for i in m["icons"]}
    assert {"192x192", "512x512"} <= sizes
    assert any(i.get("purpose") == "maskable" for i in m["icons"])
    for icon in m["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]
    assert client.get("/static/icons/apple-touch-icon.png").status_code == 200


def test_service_worker_is_versioned_and_never_caches_money(client: TestClient) -> None:
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert resp.headers["cache-control"] == "no-cache"
    assert "__VERSION__" not in resp.text
    from app.routers.ui import ASSET_VERSION

    assert ASSET_VERSION in resp.text
    # Only the shell and static files are cached; /api, /q, /p pass straight through.
    assert 'url.pathname === "/" || url.pathname.startsWith("/static/")' in resp.text


def test_app_page_links_the_manifest_and_icon(client: TestClient) -> None:
    page = client.get("/").text
    assert 'rel="manifest" href="/manifest.webmanifest"' in page
    assert 'rel="apple-touch-icon"' in page


def test_setup_saves_the_profile_and_reminder_choice(client: TestClient) -> None:
    ws = client.put(
        "/api/workspace",
        json={**GOOD, "trade": " Plumber ", "invoice_note": "Thanks for choosing Dave's!",
              "auto_chase": True},
    ).json()  # fmt: skip
    assert ws["trade"] == "Plumber"
    assert ws["invoice_note"] == "Thanks for choosing Dave's!"
    assert ws["auto_chase"] is True
    assert client.get("/api/chase/settings").json()["auto_chase"] is True
    assert "Thanks for choosing Dave" in client.get("/").text  # the record screen's placeholder


def test_an_older_form_leaves_the_profile_alone(client: TestClient) -> None:
    """Settings saves without trade, note or reminders: none of them may be wiped."""
    client.put(
        "/api/workspace",
        json={**GOOD, "trade": "Plumber", "invoice_note": "Ta!", "auto_chase": True},
    )
    ws = client.put("/api/workspace", json={**GOOD, "business_name": "Dave & Sons"}).json()
    assert (ws["trade"], ws["invoice_note"], ws["auto_chase"]) == ("Plumber", "Ta!", True)
    ws = client.put("/api/workspace", json={**GOOD, "trade": "", "invoice_note": ""}).json()
    assert (ws["trade"], ws["invoice_note"]) == ("", "")


def test_profile_limits(client: TestClient) -> None:
    for bad in ({"trade": "x" * 41}, {"invoice_note": "x" * 1001}):
        assert client.put("/api/workspace", json={**GOOD, **bad}).status_code == 422, bad


def test_the_onboarding_flow_is_on_the_page(client: TestClient) -> None:
    page = client.get("/").text
    for step in ("Who's sending the invoices?", "How you like to bill", "Your deal rules",
                 "Getting paid", "You're all set"):  # fmt: skip
        assert step in page or step.replace("'", "&#39;") in page, step
    assert 'aria-label="A preview of your invoices"' in page
