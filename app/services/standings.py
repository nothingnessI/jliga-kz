from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.tournament_standing import TournamentStanding
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.enums import MatchStatus, MatchEventType


class StandingsService:
    @staticmethod
    async def recalculate_standings(db: AsyncSession, tournament_id: int):
        """Полный пересчет турнирной таблицы (И, В, Н, П, МЗ, МП, ЖК, КК, '0')."""
        stmt = select(TournamentStanding).where(TournamentStanding.tournament_id == tournament_id)
        standings = {s.team_id: s for s in (await db.execute(stmt)).scalars().all()}

        # Сброс статистики
        for s in standings.values():
            s.games_played = 0
            s.wins = 0
            s.draws = 0
            s.losses = 0
            s.goals_for = 0
            s.goals_against = 0
            s.yellow_cards = 0
            s.red_cards = 0
            s.clean_sheets = 0
            s.points = 0

        # Берем завершенные матчи
        match_stmt = select(Match).where(
            and_(Match.tournament_id == tournament_id, Match.status == MatchStatus.FINISHED)
        )
        matches = (await db.execute(match_stmt)).scalars().all()

        for m in matches:
            if m.home_score is None or m.away_score is None:
                continue
            home, away = standings.get(m.home_team_id), standings.get(m.away_team_id)
            if not home or not away:
                continue

            home.games_played += 1
            away.games_played += 1
            home.goals_for += m.home_score
            home.goals_against += m.away_score
            away.goals_for += m.away_score
            away.goals_against += m.home_score

            if m.home_score > m.away_score:
                home.wins += 1;
                home.points += 3;
                away.losses += 1
            elif m.home_score < m.away_score:
                away.wins += 1;
                away.points += 3;
                home.losses += 1
            else:
                home.draws += 1;
                away.draws += 1
                home.points += 1;
                away.points += 1

            if m.away_score == 0: home.clean_sheets += 1
            if m.home_score == 0: away.clean_sheets += 1

        # Считаем карточки
        event_stmt = select(MatchEvent).join(Match).where(
            and_(Match.tournament_id == tournament_id, Match.status == MatchStatus.FINISHED)
        )
        for event in (await db.execute(event_stmt)).scalars().all():
            team = standings.get(event.team_id)
            if not team: continue
            if event.event_type == MatchEventType.YELLOW_CARD:
                team.yellow_cards += 1
            elif event.event_type == MatchEventType.RED_CARD:
                team.red_cards += 1

        await db.commit()
        return list(standings.values())