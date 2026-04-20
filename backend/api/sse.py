from __future__ import annotations
import asyncio
import json
import logging
import uuid
from dataclasses import asdict
from typing import AsyncIterator

from backend.game.state import GameEvent

logger = logging.getLogger(__name__)


class SSEManager:
    """
    Manages a set of asyncio Queues, one per connected browser client.
    GameEngine pushes events here; the SSE route drains them.
    """

    def __init__(self) -> None:
        self._clients: dict[str, asyncio.Queue] = {}

    def add_client(self) -> tuple[str, asyncio.Queue]:
        client_id = str(uuid.uuid4())[:8]
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._clients[client_id] = q
        logger.debug("SSE client connected: %s (total=%d)", client_id, len(self._clients))
        return client_id, q

    def remove_client(self, client_id: str) -> None:
        self._clients.pop(client_id, None)
        logger.debug("SSE client disconnected: %s (total=%d)", client_id, len(self._clients))

    async def broadcast(self, event: GameEvent) -> None:
        """Push event to all connected clients."""
        if not self._clients:
            return
        payload = _serialize(event)
        for q in list(self._clients.values()):
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning("SSE queue full, dropping event")

    async def event_stream(self, client_id: str, q: asyncio.Queue) -> AsyncIterator[str]:
        """
        Async generator consumed by the SSE route.
        Yields SSE-formatted strings until the game ends or client disconnects.
        """
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Send keep-alive comment
                    yield ": keep-alive\n\n"
                    continue

                yield f"data: {payload}\n\n"

                # Stop streaming after game_end event
                if '"game_end"' in payload:
                    break
        finally:
            self.remove_client(client_id)


def _serialize(event: GameEvent) -> str:
    d = {
        "type": event.type.value,
        "round": event.round,
        "data": event.data,
        "public": event.public,
        "timestamp": event.timestamp,
    }
    return json.dumps(d, ensure_ascii=False)
