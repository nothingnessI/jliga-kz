from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.api import deps
from app.crud.stadium import stadium as crud_stadium
from app.schemas.stadium import StadiumCreate, StadiumRead, StadiumUpdate
from app.models.user import User

router = APIRouter()

@router.get("/", response_model=List[StadiumRead])
async def read_stadiums(
    db: AsyncSession = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
) -> Any:
    """Получить список стадионов."""
    return await crud_stadium.get_multi(db, skip=skip, limit=limit)

@router.post("/", response_model=StadiumRead, status_code=status.HTTP_201_CREATED)
async def create_stadium(
    *,
    db: AsyncSession = Depends(get_db),
    stadium_in: StadiumCreate,
    current_user: User = Depends(deps.get_current_admin), # Только админ может создавать
) -> Any:
    """Создать новый стадион."""
    stadium = await crud_stadium.create(db, obj_in=stadium_in)
    return stadium

@router.get("/{stadium_id}", response_model=StadiumRead)
async def read_stadium(
    stadium_id: int,
    db: AsyncSession = Depends(get_db),
) -> Any:
    """Получить стадион по ID."""
    stadium = await crud_stadium.get(db, stadium_id)
    if not stadium:
        raise HTTPException(status_code=404, detail="Стадион не найден")
    return stadium

@router.patch("/{stadium_id}", response_model=StadiumRead)
async def update_stadium(
    *,
    db: AsyncSession = Depends(get_db),
    stadium_id: int,
    stadium_in: StadiumUpdate,
    current_user: User = Depends(deps.get_current_admin),
) -> Any:
    """Обновить стадион."""
    stadium = await crud_stadium.get(db, stadium_id)
    if not stadium:
        raise HTTPException(status_code=404, detail="Стадион не найден")
    return await crud_stadium.update(db, db_obj=stadium, obj_in=stadium_in)