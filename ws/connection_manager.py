from fastapi import WebSocket
from typing import Dict,Any
import logging

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]

    async def send(self, message: Any, client_id: str):
        ws = self.active_connections.get(client_id)
        if ws:
            await ws.send_json(message)
