
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MatchEventType, MatchStatus
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.player import Player


class StatsService:
    """Сервис статистики турниров J-LIGA."""

    @staticmethod
    async def get_top_scorers(
        db: AsyncSession,
        tournament_id: int,
        limit: int = 10,
    ) -> list[dict]:
        """
        Возвращает лучших бомбардиров турнира.

        Учитываются:
        - голы с игры;
        - голы с пенальти.

        Не учитываются:
        - автоголы;
        - события незавершённых матчей.

        Формат результата:
        [
            {
                "player_id": 1,
                "name": "Иван Иванов",
                "goals": 12,
            }
        ]
        """

        if limit <= 0:
            return []

        goals_count = func.count(
            MatchEvent.id
        ).label("goals")

        stmt = (
            select(
                Player.id.label("player_id"),
                Player.first_name,
                Player.last_name,
                goals_count,
            )
            .join(
                MatchEvent,
                MatchEvent.player_id == Player.id,
            )
            .join(
                Match,
                Match.id == MatchEvent.match_id,
            )
            .where(
                Match.tournament_id == tournament_id,
                Match.status == MatchStatus.FINISHED,
                MatchEvent.event_type.in_(
                    [
                        MatchEventType.GOAL,
                        MatchEventType.PENALTY_GOAL,
                    ]
                ),
            )
            .group_by(
                Player.id,
                Player.first_name,
                Player.last_name,
            )
            .order_by(
                desc(goals_count),
                Player.last_name.asc(),
                Player.first_name.asc(),
                Player.id.asc(),
            )
            .limit(limit)
        )

        result = await db.execute(stmt)

        scorers = []

        for row in result.all():
            first_name = (
                row.first_name or ""
            ).strip()

            last_name = (
                row.last_name or ""
            ).strip()

            full_name = " ".join(
                part
                for part in (first_name, last_name)
                if part
            )

            scorers.append(
                {
                    "player_id": row.player_id,
                    "name": full_name,
                    "goals": int(row.goals),
                }
            )

        return scorers
