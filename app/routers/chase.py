"""The Chase Agent: run it now, review its drafts, and choose approval or automatic mode."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Reminder, Workspace
from app.paypal import PayPalClient, PayPalError, get_paypal
from app.routers.invoices import current_workspace
from app.services import chase, clock
from app.services.chase import WriterFactory
from app.services.invoice_actions import ActionRefused
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api", tags=["chase"])


def get_writer_factory(
    settings: Settings = Depends(get_settings), paypal: PayPalClient = Depends(get_paypal)
) -> WriterFactory:
    """The ADK agent with PayPal's MCP tools. Overridden in tests."""
    return chase.chase_writer(settings, paypal)


@router.post("/chase/run")
async def run_now(
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    paypal: PayPalClient = Depends(get_paypal),
    writers: WriterFactory = Depends(get_writer_factory),
) -> dict[str, Any]:
    """The "Run Chase Agent now" button (the hourly cron job calls the same code)."""
    return await chase.run(
        session, paypal, ws, writers, today=clock.today_for(ws), now=clock.now_for(ws)
    )


@router.get("/reminders")
async def pending_reminders(
    session: AsyncSession = Depends(get_session), ws: Workspace = Depends(current_workspace)
) -> list[dict[str, Any]]:
    return await chase.pending(session, ws, clock.today_for(ws))


class SendIn(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


async def _reminder(session: AsyncSession, ws: Workspace, reminder_id: str) -> Reminder:
    r = await session.get(Reminder, reminder_id)
    if r is None or r.workspace_id != ws.id:
        raise HTTPException(404, "reminder not found")
    return r


@router.post("/reminders/{reminder_id}/send")
async def send_reminder(
    reminder_id: str,
    data: SendIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    paypal: PayPalClient = Depends(get_paypal),
) -> dict[str, str]:
    r = await _reminder(session, ws, reminder_id)
    if data.note is not None and not data.note.strip():
        raise HTTPException(422, "The reminder can't be empty.")
    try:
        await chase.send(session, paypal, ws, r, note=data.note, now=clock.now_for(ws))
    except ActionRefused as exc:
        raise HTTPException(409, str(exc)[:1].upper() + str(exc)[1:] + ".") from exc
    except PayPalError as exc:
        raise HTTPException(502, exc.human()) from exc
    return {"status": r.status}


@router.post("/reminders/{reminder_id}/skip")
async def skip_reminder(
    reminder_id: str,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
) -> dict[str, str]:
    r = await _reminder(session, ws, reminder_id)
    try:
        await chase.skip(session, r)
    except ActionRefused as exc:
        raise HTTPException(409, str(exc)[:1].upper() + str(exc)[1:] + ".") from exc
    return {"status": r.status}


class ChaseSettings(BaseModel):
    auto_chase: bool


@router.get("/chase/settings", response_model=ChaseSettings)
async def read_chase_settings(ws: Workspace = Depends(current_workspace)) -> ChaseSettings:
    return ChaseSettings(auto_chase=ws.auto_chase)


@router.put("/chase/settings", response_model=ChaseSettings)
async def save_chase_settings(
    data: ChaseSettings,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
) -> ChaseSettings:
    ws.auto_chase = data.auto_chase
    await session.commit()
    return ChaseSettings(auto_chase=ws.auto_chase)
