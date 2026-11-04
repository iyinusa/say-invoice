from __future__ import annotations

import os

# Before any app import: tests never read the developer's .env or touch PayPal.
os.environ["SI_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SI_PAYPAL_CLIENT_ID"] = "test-client"
os.environ["SI_PAYPAL_CLIENT_SECRET"] = "test-secret"
os.environ["SI_PAYPAL_WEBHOOK_ID"] = "WH-TEST"
os.environ["SI_GOOGLE_API_KEY"] = ""
os.environ["SI_RECONCILE_INTERVAL_S"] = "0"  # no background PayPal polling in tests


from collections.abc import Iterator  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture
def client() -> Iterator[TestClient]:
    """The app with a fresh in-memory database, created inside the app's own event loop."""
    import app.models  # noqa: F401 - register tables
    from app.db import Base, get_engine, reset_engine_for_tests
    from app.main import app as fastapi_app

    async def create_tables() -> None:
        async with get_engine().begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose() -> None:
        await get_engine().dispose()

    reset_engine_for_tests()
    with TestClient(fastapi_app) as test_client:
        test_client.portal.call(create_tables)  # type: ignore[union-attr]
        yield test_client
        test_client.portal.call(dispose)  # type: ignore[union-attr]
    reset_engine_for_tests()
