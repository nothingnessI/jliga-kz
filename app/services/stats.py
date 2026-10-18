from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.match_event import MatchEvent
from app.models.match import Match
from app.models.player import Player
from app.models.enums import MatchEventType, MatchStatus


class StatsService:
    @staticmethod
    async def get_top_scorers(db: AsyncSession, tournament_id: int, limit: int = 10):
        """Возвращает список лучших бомбардиров турнира."""
        # Считаем голы и пенальти (автоголы не идут в зачет игроку)
        stmt = (
            select(
                Player.id,
                Player.first_name,
                Player.last_name,
                func.count(MatchEvent.id).label("goals")
            )
            .join(MatchEvent, MatchEvent.player_id == Player.id)
            .join(Match, MatchEvent.match_id == Match.id)
            .where(
                Match.tournament_id == tournament_id,
                Match.status == MatchStatus.FINISHED,
                MatchEvent.event_type.in_([MatchEventType.GOAL, MatchEventType.PENALTY_GOAL])
            )
            .group_by(Player.id, Player.first_name, Player.last_name)
            .order_by(desc("goals"))
            .limit(limit)
        )

        result = await db.execute(stmt)

        # Возвращаем список словарей, чтобы Jinja2 легко могла их вывести
        return [
            {
                "player_id": row.id,
                "name": f"{row.first_name} {row.last_name}",
                "goals": row.goals
            }
            for row in result.all()
        ]