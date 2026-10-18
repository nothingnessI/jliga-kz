from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.match import MatchService
from app.schemas.match_event import MatchEventCreate, MatchEventRead
from app.db.session import get_db
from app.api import deps
from app.crud.match import match as crud_match
from app.schemas.match import MatchCreate, MatchRead, MatchUpdate, MatchReadWithTeams
from app.models.user import User

router = APIRouter()

@router.get("/tournament/{tournament_id}", response_model=List[MatchReadWithTeams])
async def read_matches_by_tournament(
    tournament_id: int, db: AsyncSession = Depends(get_db)
) -> Any:
    """Получить расписание матчей конкретного турнира (с названиями команд)."""
    matches = await crud_match.get_by_tournament(db, tournament_id=tournament_id)
    return matches

@router.post("/", response_model=MatchRead, status_code=status.HTTP_201_CREATED)
async def create_match(
    *,
    db: AsyncSession = Depends(get_db),
    match_in: MatchCreate,
    current_user: User = Depends(deps.get_current_admin),
) -> Any:
    """Назначить матч (только админ)."""
    if match_in.home_team_id == match_in.away_team_id:
        raise HTTPException(status_code=400, detail="Команда не может играть сама с собой")
    return await crud_match.create(db, obj_in=match_in)

router.post("/{match_id}/events", response_model=MatchEventRead)
async def add_match_event(
    match_id: int,
    event_in: MatchEventCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_admin),
):
    """Добавить гол/карточку (счет обновится автоматически)."""
    try:
        return await MatchService.add_event_and_update_score(
            db, match_id=match_id, event_data=event_in.model_dump()
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@router.post("/{match_id}/finish")
async def finish_match(
    match_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(deps.get_current_admin),
):
    """Завершить матч и пересчитать таблицу."""
    try:
        await MatchService.finish_match(db, match_id=match_id)
        return {"status": "finished", "message": "Матч завершен, таблица обновлена"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))