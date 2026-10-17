from fastapi import APIRouter
from app.api.v1.endpoints import auth, teams, players

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["Авторизация"])
api_router.include_router(teams.router, prefix="/teams", tags=["Команды"])
api_router.include_router(players.router, prefix="/players", tags=["Игроки"])