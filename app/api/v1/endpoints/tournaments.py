from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.match import MatchService
from app.crud.match import match as crud_match
from app.schemas.match import MatchCreate
from app.db.session import get_db
from app.api import deps
from app.crud.tournament import tournament as crud_tournament
from app.crud.tournament_team import tournament_team as crud_tt
from app.schemas.tournament import TournamentCreate, TournamentRead, TournamentUpdate
from app.models.user import User
from app.schemas.tournament_team import TournamentTeamCreate, TournamentTeamRead
from app.crud.tournament_standing import tournament_standing as crud_standing
from app.schemas.tournament_standing import TournamentStandingReadWithTeam
from app.services.standings import StandingsService

router = APIRouter()


@router.get("/", response_model=List[TournamentRead])
async def read_tournaments(
        db: AsyncSession = Depends(get_db),
        skip: int = 0,
        limit: int = 100
) -> Any:
    """Получить список всех турниров."""
    return await crud_tournament.get_multi(db, skip=skip, limit=limit)


@router.post("/", response_model=TournamentRead, status_code=status.HTTP_201_CREATED)
async def create_tournament(
        *,
        db: AsyncSession = Depends(get_db),
        tournament_in: TournamentCreate,
        current_user: User = Depends(deps.get_current_admin),
) -> Any:
    """Создать новый турнир (только админ)."""
    return await crud_tournament.create(db, obj_in=tournament_in)


@router.get("/{tournament_id}", response_model=TournamentRead)
async def read_tournament(tournament_id: int, db: AsyncSession = Depends(get_db)) -> Any:
    """Получить информацию о турнире."""
    tournament = await crud_tournament.get(db, tournament_id)
    if not tournament:
        raise HTTPException(status_code=404, detail="Турнир не найден")
    return tournament


@router.post(
    "/{tournament_id}/teams/{team_id}",
    response_model=TournamentTeamRead,  # <-- явно указываем модель ответа
    status_code=status.HTTP_201_CREATED,
)
async def add_team_to_tournament(
        tournament_id: int,
        team_id: int,
        group_name: Optional[str] = Query(None, description="Название группы (например, A)"),  # <-- Query()
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(deps.get_current_admin),
) -> Any:
    """Добавить команду в турнир (заявка на участие)."""
    existing_teams = await crud_tt.get_by_tournament(db, tournament_id=tournament_id)
    if any(tt.team_id == team_id for tt in existing_teams):
        raise HTTPException(status_code=400, detail="Команда уже участвует в этом турнире")

    # Формируем Pydantic-схему, а не словарь
    obj_in = TournamentTeamCreate(
        tournament_id=tournament_id,
        team_id=team_id,
        group_name=group_name,
    )
    return await crud_tt.create(db, obj_in=obj_in)

@router.get("/{tournament_id}/standings", response_model=list[TournamentStandingReadWithTeam])
async def get_standings(tournament_id: int, db: AsyncSession = Depends(get_db)):
    """Получить турнирную таблицу."""
    await StandingsService.recalculate_standings(db, tournament_id)
    return await crud_standing.get_by_tournament(db, tournament_id=tournament_id)


@router.post("/{tournament_id}/generate-calendar", status_code=status.HTTP_201_CREATED)
async def generate_calendar(
        tournament_id: int,
        db: AsyncSession = Depends(get_db),
        current_user: User = Depends(deps.get_current_admin),
):
    """Автоматически сгенерировать календарь матчей для всех команд турнира."""
    # 1. Получаем все команды турнира
    tournament_teams = await crud_tt.get_by_tournament(db, tournament_id=tournament_id)
    if len(tournament_teams) < 2:
        raise HTTPException(status_code=400, detail="В турнире должно быть минимум 2 команды")

    team_ids = [tt.team_id for tt in tournament_teams]

    # 2. Генерируем пары (каждый с каждым)
    pairs = MatchService.generate_round_robin(team_ids)

    # 3. Создаем матчи в БД
    created_matches = []
    for home_id, away_id in pairs:
        match_in = MatchCreate(
            tournament_id=tournament_id,
            home_team_id=home_id,
            away_team_id=away_id,
            status="SCHEDULED"
        )
        match = await crud_match.create(db, obj_in=match_in)
        created_matches.append(match)

    return {"message": f"Успешно создано {len(created_matches)} матчей", "count": len(created_matches)}