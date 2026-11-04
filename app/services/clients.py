"""Client memory: "Mrs Patel" finds the Mrs Patel you've invoiced before.

Matching is plain code: titles dropped, every spoken name word must be in the
saved name ("Patel" matches "Priya Patel" and "Mrs Patel"). One match fills the
card; several are offered as choices; none leaves the card to ask.
"""

from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Client, Workspace

TITLES = {
    "mr",
    "mrs",
    "ms",
    "miss",
    "mx",
    "dr",
    "sr",
    "sra",
    "srta",
    "mme",
    "m",
    "herr",
    "frau",
    "pan",
    "pani",
}


def _recent(ws: Workspace):  # type: ignore[no-untyped-def]
    newest = Client.created_at.desc()
    return select(Client).where(Client.workspace_id == ws.id).order_by(newest).limit(500)


def name_words(name: str) -> set[str]:
    words = re.findall(r"[\w'-]+", (name or "").lower())
    return {w for w in words if w not in TITLES and len(w) > 1}


async def find(
    session: AsyncSession, ws: Workspace, name: str, email: str
) -> tuple[Client | None, list[Client]]:
    """(the one saved client this must be, or None; other possibilities to offer)."""
    if email:
        known = await session.scalar(
            select(Client).where(Client.workspace_id == ws.id, Client.email == email.lower())
        )
        return known, []
    spoken = name_words(name)
    if not spoken:
        return None, []
    clients = await session.scalars(
        select(Client)
        .where(Client.workspace_id == ws.id)
        .order_by(Client.created_at.desc())
        .limit(500)
    )
    matches = [c for c in clients if spoken <= name_words(c.name)]
    if len(matches) == 1:
        return matches[0], []
    return None, matches[:5]


async def search(session: AsyncSession, ws: Workspace, q: str, limit: int = 8) -> list[Client]:
    q = q.strip().lower()
    clients = await session.scalars(
        select(Client)
        .where(Client.workspace_id == ws.id)
        .order_by(Client.created_at.desc())
        .limit(500)
    )
    if not q:
        return list(clients)[:limit]
    return [c for c in clients if q in c.name.lower() or q in c.email][:limit]


def view(c: Client) -> dict[str, str]:
    return {"name": c.name, "email": c.email, "language": c.language}
