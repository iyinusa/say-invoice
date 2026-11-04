"""Each workspace has one Deal Policy, created with sensible defaults on first use."""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deals.guard import Policy
from app.models import DealPolicy, Event, Workspace
from app.schemas.policy import PolicyIn, PolicyOut

_FIELDS = tuple(PolicyIn.model_fields)


async def get_policy(session: AsyncSession, ws: Workspace) -> DealPolicy:
    row = await session.scalar(select(DealPolicy).where(DealPolicy.workspace_id == ws.id))
    if row is None:
        defaults = Policy()  # one source of truth for the defaults
        row = DealPolicy(workspace_id=ws.id, **{f: getattr(defaults, f) for f in _FIELDS})
        session.add(row)
        await session.commit()
    return row


async def update_policy(session: AsyncSession, ws: Workspace, data: PolicyIn) -> DealPolicy:
    row = await get_policy(session, ws)
    before = to_out(row).model_dump(mode="json")
    for f in _FIELDS:
        setattr(row, f, getattr(data, f))
    after = to_out(row).model_dump(mode="json")
    changed = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    if changed:
        # The audit trail shows when the rules changed, next to the deals they governed.
        session.add(Event(workspace_id=ws.id, kind="policy_changed", data=changed))
    await session.commit()
    return row


def to_out(row: DealPolicy) -> PolicyOut:
    def norm(v: object) -> object:
        # Two places always, whether the value was just set or read back from the DB.
        return v.quantize(Decimal("0.01")) if isinstance(v, Decimal) else v

    return PolicyOut(**{f: norm(getattr(row, f)) for f in _FIELDS})
