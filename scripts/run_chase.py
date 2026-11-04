"""The Chase Agent's hourly run (a Render Cron Job; see render.yaml).

    uv run python -m scripts.run_chase

Same code as the app's "Run Chase Agent now" button, for every workspace.
Drafts wait for approval unless a workspace turned on automatic chasing.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from app.db import get_engine, get_sessionmaker
from app.logging_config import configure_logging, get_logger
from app.models import Workspace
from app.paypal import close_paypal, get_paypal
from app.services import chase, clock
from app.settings import get_settings


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.env)
    log = get_logger("chase_cron")
    if not settings.paypal_configured:
        log.warning("chase_skipped", reason="PayPal is not configured")
        return
    paypal = get_paypal()
    try:
        async with get_sessionmaker()() as session:
            workspaces = list(await session.scalars(select(Workspace)))
            for ws in workspaces:
                summary = await chase.run(
                    session,
                    paypal,
                    ws,
                    chase.chase_writer(settings, paypal),
                    today=clock.today_for(ws),
                    now=clock.now_for(ws),
                )
                print(f"{ws.business_name}: {summary['due']} due, {summary['drafted']} drafted, "
                      f"{summary['sent']} sent")  # fmt: skip
    finally:
        await close_paypal()
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
