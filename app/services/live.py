"""In-process pub/sub for live updates (Server-Sent Events) to the tradesperson's phone.

One Render instance serves the app, so a process-local broker is enough. With
more instances this becomes Postgres LISTEN/NOTIFY; the publish/subscribe
calls stay the same.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.logging_config import get_logger

log = get_logger("live")


class Broker:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    @contextmanager
    def subscribe(self, workspace_id: str) -> Iterator[asyncio.Queue[dict[str, Any]]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._subscribers[workspace_id].add(queue)
        try:
            yield queue
        finally:
            self._subscribers[workspace_id].discard(queue)
            if not self._subscribers[workspace_id]:
                del self._subscribers[workspace_id]

    def publish(self, workspace_id: str, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers.get(workspace_id, ())):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A phone that stopped reading: drop the event, it re-syncs on reconnect.
                log.warning("live_queue_full", workspace_id=workspace_id)

    def subscriber_count(self, workspace_id: str) -> int:
        return len(self._subscribers.get(workspace_id, ()))


broker = Broker()
