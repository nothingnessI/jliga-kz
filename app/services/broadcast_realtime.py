
import asyncio
import logging
from collections import defaultdict

from fastapi import WebSocket

from app.schemas.broadcast import BroadcastStateResponse


logger = logging.getLogger(__name__)


class BroadcastRealtimeManager:
    """
    WebSocket-соединения по ID трансляции.

    Важно: эта реализация работает в пределах
    одного процесса FastAPI.
    """

    def __init__(self) -> None:
        self._connections: dict[int, set[WebSocket]] = (
            defaultdict(set)
        )
        self._locks: dict[int, asyncio.Lock] = (
            defaultdict(asyncio.Lock)
        )

    async def connect(
        self,
        session_id: int,
        websocket: WebSocket,
    ) -> None:
        await websocket.accept()

        async with self._locks[session_id]:
            self._connections[session_id].add(websocket)

    async def disconnect(
        self,
        session_id: int,
        websocket: WebSocket,
    ) -> None:
        async with self._locks[session_id]:
            connections = self._connections.get(session_id)

            if connections is None:
                return

            connections.discard(websocket)

            if not connections:
                self._connections.pop(session_id, None)

    async def broadcast_state(
        self,
        session_id: int,
        state: BroadcastStateResponse,
    ) -> None:
        message = {
            "type": "broadcast.state",
            "data": state.model_dump(mode="json"),
        }

        async with self._locks[session_id]:
            connections = list(
                self._connections.get(session_id, ())
            )

        if not connections:
            return

        results = await asyncio.gather(
            *[
                websocket.send_json(message)
                for websocket in connections
            ],
            return_exceptions=True,
        )

        for websocket, result in zip(
            connections,
            results,
        ):
            if isinstance(result, BaseException):
                logger.warning(
                    "Broadcast WebSocket send failed: %s",
                    result,
                )
                await self.disconnect(
                    session_id,
                    websocket,
                )


broadcast_realtime = BroadcastRealtimeManager()
