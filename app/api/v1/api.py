
"""
Главный маршрутизатор API v1 проекта J-LIGA.
"""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    teams,
    players,
    tournaments,
    matches,
    stadiums,
    coach,
)

from app.api.v1.broadcast import create_broadcast_router

from app.api.deps import get_current_admin
from app.db.session import get_db
from app.services.broadcast_auth import (
    authorize_broadcast_viewer,
)


api_router = APIRouter()


# ==========================================
# АВТОРИЗАЦИЯ
# ==========================================

api_router.include_router(
    auth.router,
    prefix="/auth",
    tags=["Авторизация"],
)


# ==========================================
# КОМАНДЫ
# ==========================================

api_router.include_router(
    teams.router,
    prefix="/teams",
    tags=["Команды"],
)


# ==========================================
# ИГРОКИ
# ==========================================

api_router.include_router(
    players.router,
    prefix="/players",
    tags=["Игроки"],
)


# ==========================================
# ТУРНИРЫ
# ==========================================

api_router.include_router(
    tournaments.router,
    prefix="/tournaments",
    tags=["Турниры"],
)


# ==========================================
# МАТЧИ
# ==========================================

api_router.include_router(
    matches.router,
    prefix="/matches",
    tags=["Матчи"],
)


# ==========================================
# СТАДИОНЫ
# ==========================================

api_router.include_router(
    stadiums.router,
    prefix="/stadiums",
    tags=["Стадионы"],
)


# ==========================================
# ТРЕНЕРСКИЕ ЗАЯВКИ
# ==========================================

api_router.include_router(
    coach.router,
    prefix="/coach",
    tags=["Тренерские заявки"],
)


# ==========================================
# J-LIGA BROADCAST STUDIO
# ==========================================

broadcast_router = create_broadcast_router(
    get_db=get_db,
    require_operator=get_current_admin,
    authorize_viewer=authorize_broadcast_viewer,
)

api_router.include_router(broadcast_router)
