from fastapi import APIRouter
from app.api.v1.endpoints import auth, teams, players, tournaments, matches, stadiums

api_router = APIRouter()

api_router.include_router(auth.router, prefix="/auth", tags=["Авторизация"])
api_router.include_router(teams.router, prefix="/teams", tags=["Команды"])
api_router.include_router(players.router, prefix="/players", tags=["Игроки"])
api_router.include_router(tournaments.router, prefix="/tournaments", tags=["Турниры"])
api_router.include_router(matches.router, prefix="/matches", tags=["Матчи"])
api_router.include_router(stadiums.router, prefix="/stadiums", tags=["Стадионы"])