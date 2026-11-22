
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket

from app.api.deps import get_current_admin
from app.api.v1.broadcast import create_broadcast_router
from app.api.v1.broadcast_events import (
    router as broadcast_events_router,
)
from app.api.v1.broadcast_multistream import (
    router as broadcast_multistream_router,
)
from app.api.v1.broadcast_presentation import (
    router as broadcast_presentation_router,
)
from app.api.v1.endpoints import (
    auth,
    matches,
    players,
    stadiums,
    teams,
    tournaments,
)
from app.db.session import get_db


api_router = APIRouter()


# ========================================
# СУЩЕСТВУЮЩИЕ API
# ========================================

api_router.include_router(
    auth.router,
    prefix="/auth",
    tags=["Авторизация"],
)

api_router.include_router(
    teams.router,
    prefix="/teams",
    tags=["Команды"],
)

api_router.include_router(
    players.router,
    prefix="/players",
    tags=["Игроки"],
)

api_router.include_router(
    tournaments.router,
    prefix="/tournaments",
    tags=["Турниры"],
)

api_router.include_router(
    matches.router,
    prefix="/matches",
    tags=["Матчи"],
)

api_router.include_router(
    stadiums.router,
    prefix="/stadiums",
    tags=["Стадионы"],
)


# ========================================
# PUBLIC WEBSOCKET AUTH
# ========================================

async def authorize_broadcast_viewer(
    websocket: WebSocket,
    session_id: int,
) -> bool:
    """
    Read-only WebSocket.

    OBS/vMix могут подключаться без Origin.
    Браузерный Origin должен соответствовать Host.

    Для закрытых трансляций нужна дополнительная
    проверка зрительского доступа.
    """
    origin = websocket.headers.get("origin")

    if not origin:
        return True

    parsed = urlsplit(origin)

    host = websocket.headers.get(
        "host",
        "",
    ).lower()

    return (
        parsed.scheme in ("http", "https")
        and parsed.netloc.lower() == host
    )


# ========================================
# BROADCAST API
# ========================================

api_router.include_router(
    create_broadcast_router(
        get_db=get_db,
        require_operator=get_current_admin,
        authorize_viewer=authorize_broadcast_viewer,
    )
)

api_router.include_router(
    broadcast_events_router
)

api_router.include_router(
    broadcast_presentation_router
)

api_router.include_router(
    broadcast_multistream_router
)
