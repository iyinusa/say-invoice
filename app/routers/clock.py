"""The demo clock: "simulate N days later", so a demo can show the Chase Agent climb its ladder."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.logging_config import get_logger
from app.models import Workspace
from app.routers.invoices import current_workspace
from app.services import clock
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api/clock", tags=["demo"])
log = get_logger("clock")


class ClockIn(BaseModel):
    offset_days: int = Field(ge=0, le=clock.MAX_OFFSET_DAYS)


def _view(ws: Workspace, settings: Settings) -> dict[str, object]:
    today = clock.today_for(ws)
    return {
        "enabled": settings.demo_clock_enabled,
        "offset_days": ws.clock_offset_days,
        "today": today.isoformat(),
        "today_label": f"{today:%a} {today.day} {today:%b}",
    }


@router.get("")
async def read_clock(
    ws: Workspace = Depends(current_workspace), settings: Settings = Depends(get_settings)
) -> dict[str, object]:
    return _view(ws, settings)


@router.put("")
async def set_clock(
    data: ClockIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    if not settings.demo_clock_enabled:
        raise HTTPException(403, "The demo clock is turned off for this deployment.")
    ws.clock_offset_days = data.offset_days
    await session.commit()
    log.info("demo_clock_moved", workspace_id=ws.id, offset_days=data.offset_days)
    return _view(ws, settings)
