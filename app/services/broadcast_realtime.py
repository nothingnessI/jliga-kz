
"""WebSocket-уведомления для Broadcast Studio.

Работает внутри одного процесса Uvicorn.
Для нескольких воркеров потребуется Redis Pub/Sub.
"""

import asyncio
import logging

from fastapi import WebSocket

from app.schemas.broadcast import BroadcastStateResponse


log = logging.getLogger(__name__)


class BroadcastRealtimeManager:

    def __init__(self):
        self.connections: dict[int, set[WebSocket]] = {}
        self.lock = asyncio.Lock()

    async def connect(
        self,
        session_id: int,
        websocket: WebSocket,
    ) -> None:
        await websocket.accept()

        async with self.lock:
            self.connections.setdefault(
                session_id,
                set(),
            ).add(websocket)

    async def disconnect(
        self,
        session_id: int,
        websocket: WebSocket,
    ) -> None:
        async with self.lock:
            clients = self.connections.get(
                session_id
            )

            if clients is not None:
                clients.discard(websocket)

                if not clients:
                    self.connections.pop(
                        session_id,
                        None,
                    )

    async def broadcast_state(
        self,
        session_id: int,
        state: BroadcastStateResponse,
    ) -> None:
        message = {
            "type": "broadcast.state",
            "data": state.model_dump(
                mode="json"
            ),
        }

        async with self.lock:
            clients = list(
                self.connections.get(
                    session_id,
                    (),
                )
            )

        for websocket in clients:
            try:
                await websocket.send_json(
                    message
                )

            except Exception:
                log.debug(
                    "Dropped broadcast WS client: %s",
                    session_id,
                    exc_info=True,
                )

                await self.disconnect(
                    session_id,
                    websocket,
                )


broadcast_realtime = BroadcastRealtimeManager()
