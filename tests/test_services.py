import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.models.tournament import Tournament
from app.models.team import Team
from app.models.tournament_team import TournamentTeam  # <--- ДОБАВИЛИ ИМПОРТ
from app.models.tournament_standing import TournamentStanding
from app.models.match import Match
from app.models.enums import MatchStatus, TournamentStatus
from app.services.standings import StandingsService


async def test_standings_recalculation(db: AsyncSession):
    # 1. Создаем турнир
    t = Tournament(
        name="Calc Test", season="2026", division="PRIMERA", age_category="ADULTS",
        status=TournamentStatus.ACTIVE, max_teams=4, logo_url="", tournament_format="ROUND_ROBIN"
    )
    db.add(t)
    await db.flush()

    # 2. Создаем 2 команды
    team1 = Team(name="Team A", slug="team-a", is_active=True)
    team2 = Team(name="Team B", slug="team-b", is_active=True)
    db.add_all([team1, team2])
    await db.flush()

    # 3. ДОБАВЛЯЕМ КОМАНДЫ В ТУРНИР (это было пропущено)
    db.add_all([
        TournamentTeam(tournament_id=t.id, team_id=team1.id),
        TournamentTeam(tournament_id=t.id, team_id=team2.id)
    ])
    await db.flush()

    # 4. Создаем матч: Team A выиграла 3:1
    match = Match(
        tournament_id=t.id, home_team_id=team1.id, away_team_id=team2.id,
        home_score=3, away_score=1, status=MatchStatus.FINISHED
    )
    db.add(match)
    await db.commit()

    # 5. Запускаем сервис пересчета
    await StandingsService.recalculate_standings(db, t.id)

    # 6. Проверяем таблицу
    standings = (await db.execute(
        select(TournamentStanding).where(TournamentStanding.tournament_id == t.id)
    )).scalars().all()

    assert len(standings) == 2

    # Team A должна быть первой (3 очка, 3 забитых)
    s_team1 = next(s for s in standings if s.team_id == team1.id)
    s_team2 = next(s for s in standings if s.team_id == team2.id)

    assert s_team1.points == 3
    assert s_team1.wins == 1
    assert s_team1.goals_for == 3

    assert s_team2.points == 0
    assert s_team2.losses == 1
    assert s_team2.goals_against == 3