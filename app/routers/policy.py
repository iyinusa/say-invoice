"""GET/PUT the Deal Policy: the rules the Deal Agent negotiates within."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Workspace
from app.routers.invoices import current_workspace
from app.schemas.policy import PolicyIn, PolicyOut
from app.services import policy as policies

router = APIRouter(prefix="/api/policy", tags=["policy"])


@router.get("", response_model=PolicyOut)
async def read_policy(
    session: AsyncSession = Depends(get_session), ws: Workspace = Depends(current_workspace)
) -> PolicyOut:
    return policies.to_out(await policies.get_policy(session, ws))


@router.put("", response_model=PolicyOut)
async def save_policy(
    data: PolicyIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
) -> PolicyOut:
    return policies.to_out(await policies.update_policy(session, ws, data))
