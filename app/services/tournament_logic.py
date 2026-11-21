
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MatchEventType
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.services.standings import StandingsService


logger = logging.getLogger(__name__)


class TournamentLogicService:
    """
    Турнирная логика J-LIGA.

    Использует единый StandingsService для расчёта
    таблицы, чтобы разные части проекта не
    применяли разные правила начисления очков.

    Методы не вызывают commit().
    """

    @staticmethod
    async def recalculate_standings(
        db: AsyncSession,
        tournament_id: int,
    ):
        """
        Полностью пересчитывает турнирную таблицу.

        Сохраняет прежнее имя метода для
        совместимости с существующими маршрутами.
        """
        logger.debug(
            "Пересчёт таблицы турнира %s",
            tournament_id,
        )

        return await StandingsService.recalculate_standings(
            db,
            tournament_id,
        )

    @staticmethod
    async def update_match_score_from_events(
        db: AsyncSession,
        match_id: int,
    ):
        """
        Восстанавливает счёт матча из событий.

        Учитывает:
        - обычные голы;
        - голы с пенальти;
        - автоголы в пользу соперника.

        После изменения счёта пересчитывает
        турнирную таблицу.

        ВАЖНО:
        Метод следует вызывать только для матчей,
        счёт которых полностью ведётся по событиям.
        Если результат внесён вручную без событий,
        он будет заменён рассчитанным значением.
        """

        match = await db.scalar(
            select(Match)
            .where(Match.id == match_id)
            .with_for_update()
        )

        if match is None:
            logger.warning(
                "Матч %s не найден при пересчёте счёта",
                match_id,
            )
            return

        if (
            match.home_team_id is None
            or match.away_team_id is None
        ):
            raise ValueError(
                "У матча не определены обе команды"
            )

        if match.home_team_id == match.away_team_id:
            raise ValueError(
                "Команда не может играть сама с собой"
            )

        goal_types = [
            MatchEventType.GOAL,
            MatchEventType.PENALTY_GOAL,
            MatchEventType.OWN_GOAL,
        ]

        events = (
            await db.scalars(
                select(MatchEvent).where(
                    MatchEvent.match_id == match_id,
                    MatchEvent.event_type.in_(goal_types),
                )
            )
        ).all()

        home_goals = 0
        away_goals = 0

        for event in events:
            team_id = event.team_id

            if team_id not in (
                match.home_team_id,
                match.away_team_id,
            ):
                raise ValueError(
                    f"Событие {event.id} принадлежит "
                    "команде, которая не участвует "
                    "в этом матче"
                )

            is_home = (
                team_id == match.home_team_id
            )

            # Автогол засчитывается сопернику.
            if event.event_type == MatchEventType.OWN_GOAL:
                is_home = not is_home

            if is_home:
                home_goals += 1
            else:
                away_goals += 1

        score_changed = (
            match.home_score != home_goals
            or match.away_score != away_goals
        )

        if not score_changed:
            return

        previous_score = (
            match.home_score,
            match.away_score,
        )

        match.home_score = home_goals
        match.away_score = away_goals

        await db.flush()

        logger.info(
            "Пересчитан счёт матча %s: %s:%s -> %s:%s",
            match.id,
            previous_score[0],
            previous_score[1],
            home_goals,
            away_goals,
        )

        await TournamentLogicService.recalculate_standings(
            db,
            match.tournament_id,
        )
