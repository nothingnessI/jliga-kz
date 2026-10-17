from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.api import deps
from app.crud.player import player as crud_player
from app.schemas.player import PlayerCreate, PlayerRead, PlayerUpdate
from app.models.user import User

router = APIRouter()

@router.get("/", response_model=List[PlayerRead])
async def read_players(
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
    search: str | None = Query(None, description="Поиск по фамилии"),
) -> Any:
    """Получить список игроков (с возможностью поиска по фамилии)."""
    if search:
        return await crud_player.search_by_last_name(db, query=search, limit=limit)
    return await crud_player.get_multi(db, skip=skip, limit=limit)

@router.post("/", response_model=PlayerRead, status_code=status.HTTP_201_CREATED)
async def create_player(
    *,
    db: AsyncSession = Depends(get_db),
    player_in: PlayerCreate,
    current_user: User = Depends(deps.get_current_admin),
) -> Any:
    """Добавить нового игрока в базу (только для админов)."""
    return await crud_player.create(db, obj_in=player_in)

@router.get("/{player_id}", response_model=PlayerRead)
async def read_player(player_id: int, db: AsyncSession = Depends(get_db)) -> Any:
    """Получить профиль игрока."""
    player = await crud_player.get(db, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    return player

@router.patch("/{player_id}", response_model=PlayerRead)
async def update_player(
    *,
    db: AsyncSession = Depends(get_db),
    player_id: int,
    player_in: PlayerUpdate,
    current_user: User = Depends(deps.get_current_admin),
) -> Any:
    """Обновить данные игрока (только для админов)."""
    player = await crud_player.get(db, player_id)
    if not player:
        raise HTTPException(status_code=404, detail="Игрок не найден")
    return await crud_player.update(db, db_obj=player, obj_in=player_in)