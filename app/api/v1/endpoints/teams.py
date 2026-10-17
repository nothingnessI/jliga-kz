from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.api import deps
from app.crud.team import team as crud_team
from app.schemas.team import TeamCreate, TeamRead, TeamUpdate
from app.models.user import User

router = APIRouter()

@router.get("/", response_model=List[TeamRead])
async def read_teams(
    db: AsyncSession = Depends(get_db),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
) -> Any:
    """Получить список всех команд."""
    return await crud_team.get_multi(db, skip=skip, limit=limit)

@router.post("/", response_model=TeamRead, status_code=status.HTTP_201_CREATED)
async def create_team(
    *,
    db: AsyncSession = Depends(get_db),
    team_in: TeamCreate,
    current_user: User = Depends(deps.get_current_admin), # Защита: только админ
) -> Any:
    """Создать новую команду (только для админов)."""
    if await crud_team.get_by_slug(db, slug=team_in.slug):
        raise HTTPException(status_code=400, detail="Команда с таким slug уже существует")
    return await crud_team.create(db, obj_in=team_in)

@router.get("/{team_id}", response_model=TeamRead)
async def read_team(team_id: int, db: AsyncSession = Depends(get_db)) -> Any:
    """Получить информацию о конкретной команде."""
    team = await crud_team.get(db, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Команда не найдена")
    return team

@router.patch("/{team_id}", response_model=TeamRead)
async def update_team(
    *,
    db: AsyncSession = Depends(get_db),
    team_id: int,
    team_in: TeamUpdate,
    current_user: User = Depends(deps.get_current_admin),
) -> Any:
    """Обновить данные команды (только для админов)."""
    team = await crud_team.get(db, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Команда не найдена")
    return await crud_team.update(db, db_obj=team, obj_in=team_in)

@router.delete("/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_team(
    *,
    db: AsyncSession = Depends(get_db),
    team_id: int,
    current_user: User = Depends(deps.get_current_admin),
) -> None:
    """Удалить команду (только для админов)."""
    team = await crud_team.get(db, team_id)
    if not team:
        raise HTTPException(status_code=404, detail="Команда не найдена")
    await crud_team.delete(db, db_obj=team)