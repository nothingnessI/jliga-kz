
"""
J-LIGA Broadcast Studio API.

Административные маршруты:
- создание трансляции;
- получение состояния для оператора;
- выполнение команд.

Маршрут просмотра:
- получение состояния табло без административных прав.

WebSocket:
- проверка доступа через authorize_viewer;
- отправка текущего состояния;
- получение обновлений в реальном времени.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.broadcast import (
    BroadcastCommandRequest,
    BroadcastSessionCreate,
    BroadcastStateResponse,
)
from app.services.broadcast import (
    BroadcastService,
    state_response,
)
from app.services.broadcast_realtime import broadcast_realtime


def create_broadcast_router(
    get_db: Callable[..., Any],
    require_operator: Callable[..., Any],
    authorize_viewer: Callable[
        [WebSocket, int], Awaitable[bool]
    ],
) -> APIRouter:

    router = APIRouter(
        prefix="/broadcast",
        tags=["Broadcast Studio"],
    )

    # ==========================================
    # СОЗДАНИЕ ТРАНСЛЯЦИИ — ADMIN
    # ==========================================

    @router.post(
        "/sessions",
        response_model=BroadcastStateResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_session(
        data: BroadcastSessionCreate,
        db: AsyncSession = Depends(get_db),
        operator: Any = Depends(require_operator),
    ):
        async with db.begin():
            session = await BroadcastService.create_session(
                db,
                data.match_id,
            )
            result = state_response(session)

        return result

    # ==========================================
    # СОСТОЯНИЕ ТРАНСЛЯЦИИ — ADMIN
    # ==========================================

    @router.get(
        "/sessions/{session_id}",
        response_model=BroadcastStateResponse,
    )
    async def get_session(
        session_id: int,
        db: AsyncSession = Depends(get_db),
        operator: Any = Depends(require_operator),
    ):
        session = await BroadcastService.get_session(
            db,
            session_id,
        )

        return state_response(session)

    # ==========================================
    # СОСТОЯНИЕ ТАБЛО — VIEWER
    # ==========================================

    @router.get(
        "/sessions/{session_id}/public",
        response_model=BroadcastStateResponse,
    )
    async def get_public_session(
        session_id: int,
        db: AsyncSession = Depends(get_db),
    ):
        """
        Чтение состояния трансляции без прав администратора.

        Используется HTML-оверлеем vMix.
        Не позволяет выполнять команды.

        Для production следует добавить
        проверку токена просмотра.
        """
        session = await BroadcastService.get_session(
            db,
            session_id,
        )

        return state_response(session)

    # ==========================================
    # УПРАВЛЕНИЕ ТРАНСЛЯЦИЕЙ — ADMIN
    # ==========================================

    @router.post(
        "/sessions/{session_id}/commands",
        response_model=BroadcastStateResponse,
    )
    async def execute_command(
        session_id: int,
        command: BroadcastCommandRequest,
        db: AsyncSession = Depends(get_db),
        operator: Any = Depends(require_operator),
    ):
        operator_id = getattr(operator, "id", None)

        async with db.begin():
            result = await BroadcastService.execute_command(
                db=db,
                session_id=session_id,
                command=command,
                operator_id=operator_id,
            )

        # Отправляем обновление только после COMMIT.
        await broadcast_realtime.broadcast_state(
            session_id,
            result,
        )

        return result

    # ==========================================
    # WEBSOCKET — REALTIME VIEWER
    # ==========================================

    @router.websocket(
        "/sessions/{session_id}/ws",
    )
    async def websocket_session(
        websocket: WebSocket,
        session_id: int,
        db: AsyncSession = Depends(get_db),
    ):
        allowed = await authorize_viewer(
            websocket,
            session_id,
        )

        if not allowed:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
            )

        # Проверяем существование трансляции.
        session = await BroadcastService.get_session(
            db,
            session_id,
        )

        # Менеджер принимает WebSocket-соединение.
        await broadcast_realtime.connect(
            session_id,
            websocket,
        )

        try:
            # Первое сообщение — текущее состояние.
            await websocket.send_json(
                {
                    "type": "broadcast.state",
                    "data": state_response(
                        session
                    ).model_dump(mode="json"),
                }
            )

            while True:
                # Через WebSocket нельзя управлять матчем.
                # Соединение используется только для просмотра.
                await websocket.receive_text()

        except WebSocketDisconnect:
            pass

        finally:
            await broadcast_realtime.disconnect(
                session_id,
                websocket,
            )

    return router
