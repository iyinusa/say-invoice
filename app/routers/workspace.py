"""The business itself: name, owner, trade, currency, due days, invoice note, reminders.

Set in onboarding, edited later in Settings."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.parser_rules import PAYPAL_CURRENCIES
from app.db import get_session
from app.models import Event, Workspace
from app.routers.invoices import current_workspace
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api/workspace", tags=["workspace"])


class WorkspaceIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    business_name: str = Field(min_length=1, max_length=120)
    owner_name: str = Field(default="", max_length=120)
    currency: str
    default_due_days: int = Field(ge=0, le=90)
    # Left out (None) = unchanged, so an older form can't wipe them.
    trade: str | None = Field(default=None, max_length=40)
    invoice_note: str | None = Field(default=None, max_length=1000)
    auto_chase: bool | None = None
    onboarded: bool = True

    @field_validator("currency")
    @classmethod
    def _currency(cls, v: str) -> str:
        code = v.strip().upper()
        if code not in PAYPAL_CURRENCIES:
            raise ValueError(f"PayPal can't invoice in {code or 'that currency'}")
        return code


def _view(ws: Workspace, settings: Settings) -> dict[str, object]:
    return {
        "business_name": ws.business_name,
        "owner_name": ws.owner_name,
        "currency": ws.currency,
        "default_due_days": ws.default_due_days,
        "trade": ws.trade,
        "invoice_note": ws.invoice_note,
        "auto_chase": ws.auto_chase,
        "onboarded": ws.onboarded,
        # Payments always go to the PayPal business account behind the app's credentials.
        "paypal_account": ws.owner_email or settings.paypal_merchant_email,
        "paypal_env": settings.paypal_env,
        "currencies": sorted(PAYPAL_CURRENCIES),
    }


@router.get("")
async def read_workspace(
    ws: Workspace = Depends(current_workspace), settings: Settings = Depends(get_settings)
) -> dict[str, object]:
    return _view(ws, settings)


@router.put("")
async def save_workspace(
    data: WorkspaceIn,
    session: AsyncSession = Depends(get_session),
    ws: Workspace = Depends(current_workspace),
    settings: Settings = Depends(get_settings),
) -> dict[str, object]:
    first_time = not ws.onboarded and data.onboarded
    ws.business_name = data.business_name
    ws.owner_name = data.owner_name
    ws.currency = data.currency
    ws.default_due_days = data.default_due_days
    if data.trade is not None:
        ws.trade = data.trade
    if data.invoice_note is not None:
        ws.invoice_note = data.invoice_note
    if data.auto_chase is not None:
        ws.auto_chase = data.auto_chase
    ws.onboarded = ws.onboarded or data.onboarded
    if first_time:
        session.add(
            Event(
                workspace_id=ws.id,
                kind="onboarded",
                data={"currency": ws.currency, "trade": ws.trade},
            )
        )
    await session.commit()
    return _view(ws, settings)
