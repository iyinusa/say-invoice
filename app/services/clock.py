"""The workspace's clock: real time, unless a demo has moved it forward.

Everything that asks "what's today?" (due dates, overdue, quote expiry, plan
dates, the Chase Agent's 72-hour gaps) asks here. Dates sent to PayPal (invoice
and payment dates) stay real, and stored timestamps are always real time.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

MAX_OFFSET_DAYS = 120


def offset(ws: Any) -> timedelta:
    return timedelta(days=int(getattr(ws, "clock_offset_days", 0) or 0))


def today_for(ws: Any) -> date:
    return datetime.now(UTC).date() + offset(ws)


def now_for(ws: Any) -> datetime:
    return datetime.now(UTC) + offset(ws)


def real_today() -> date:
    return datetime.now(UTC).date()
