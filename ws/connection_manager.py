import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

_QUEUE_CLOSE = object()


@dataclass
class _ClientConnectionState:
    websocket: WebSocket
    queue: asyncio.Queue[Any]
    sender_task: asyncio.Task[None]


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, _ClientConnectionState] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await self.disconnect(client_id)
        await websocket.accept()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        state = _ClientConnectionState(
            websocket=websocket,
            queue=queue,
            sender_task=asyncio.create_task(
                self._sender_loop(client_id, websocket, queue),
                name=f"ws_sender_{client_id}",
            ),
        )
        self.active_connections[client_id] = state

    async def disconnect(self, client_id: str, websocket: WebSocket | None = None):
        state = self.active_connections.get(client_id)
        if not state:
            return
        if websocket is not None and state.websocket is not websocket:
            return
        self.active_connections.pop(client_id, None)

        with contextlib.suppress(asyncio.QueueFull):
            state.queue.put_nowait(_QUEUE_CLOSE)

        state.sender_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await state.sender_task

        with contextlib.suppress(Exception):
            await state.websocket.close()

    async def send(self, message: Any, client_id: str):
        state = self.active_connections.get(client_id)
        if not state:
            return
        try:
            state.queue.put_nowait(message)
        except asyncio.QueueFull:
            logger.warning("WebSocket queue full for client=%s", client_id)
            return
        # Yield so the sender task can flush independently from the caller.
        await asyncio.sleep(0)

    async def close(self) -> None:
        client_ids = list(self.active_connections.keys())
        for client_id in client_ids:
            await self.disconnect(client_id)

    async def _sender_loop(
        self,
        client_id: str,
        websocket: WebSocket,
        queue: asyncio.Queue[Any],
    ) -> None:
        try:
            while True:
                message = await queue.get()
                if message is _QUEUE_CLOSE:
                    return
                await websocket.send_json(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("WebSocket sender stopped for client=%s error=%s", client_id, exc)
        finally:
            current = self.active_connections.get(client_id)
            if current and current.websocket is websocket:
                self.active_connections.pop(client_id, None)
