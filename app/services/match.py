
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.enums import MatchEventType, MatchStatus


class MatchService:
    """Сервис управления матчами и их событиями."""

    GOAL_EVENTS = {
        MatchEventType.GOAL,
        MatchEventType.PENALTY_GOAL,
        MatchEventType.OWN_GOAL,
    }

    RED_CARD_EVENTS = {
        MatchEventType.RED_CARD,
        MatchEventType.SECOND_YELLOW_CARD,
    }

    @staticmethod
    async def add_event_and_update_score(
        db: AsyncSession,
        match_id: int,
        event_data: dict,
    ) -> MatchEvent:
        """
        Добавляет событие матча.

        При голе обновляет счёт.
        При удалении игрока создаёт дисквалификацию.
        Сохраняет изменения одной транзакцией.
        """

        stmt = (
            select(Match)
            .where(Match.id == match_id)
            .with_for_update()
        )
        match = await db.scalar(stmt)

        if match is None:
            raise ValueError("Матч не найден")

        if match.status == MatchStatus.FINISHED:
            raise ValueError(
                "Нельзя добавлять события в завершённый матч"
            )

        # Не изменяем исходный словарь вызывающего кода.
        data = dict(event_data)
        data.pop("match_id", None)
        data.pop("id", None)

        event_type = data.get("event_type")
        player_id = data.get("player_id")
        team_id = data.get("team_id")

        # Проверяем принадлежность команды матчу.
        if team_id not in (
            match.home_team_id,
            match.away_team_id,
        ):
            raise ValueError(
                "Команда события не участвует в этом матче"
            )

        # Гол и карточки требуют указания футболиста.
        if (
            event_type in self_required_player_events()
            and not player_id
        ):
            raise ValueError(
                "Для этого события необходимо указать игрока"
            )

        # Первая и вторая жёлтые карточки.
        if event_type in (
            MatchEventType.YELLOW_CARD,
            MatchEventType.SECOND_YELLOW_CARD,
        ):
            existing_yellow = await db.scalar(
                select(MatchEvent)
                .where(
                    MatchEvent.match_id == match_id,
                    MatchEvent.player_id == player_id,
                    MatchEvent.event_type
                    == MatchEventType.YELLOW_CARD,
                )
                .limit(1)
            )

            if event_type == MatchEventType.YELLOW_CARD:
                if existing_yellow:
                    event_type = (
                        MatchEventType.SECOND_YELLOW_CARD
                    )
                    data["event_type"] = event_type

            elif existing_yellow is None:
                raise ValueError(
                    "Нельзя назначить вторую жёлтую карточку "
                    "без первой"
                )

            if event_type == MatchEventType.SECOND_YELLOW_CARD:
                existing_second = await db.scalar(
                    select(MatchEvent.id)
                    .where(
                        MatchEvent.match_id == match_id,
                        MatchEvent.player_id == player_id,
                        MatchEvent.event_type
                        == MatchEventType.SECOND_YELLOW_CARD,
                    )
                    .limit(1)
                )

                if existing_second is not None:
                    raise ValueError(
                        "Игрок уже получил вторую жёлтую "
                        "карточку в этом матче"
                    )

        event = MatchEvent(
            **data,
            match_id=match_id,
        )
        db.add(event)

        if event_type in MatchService.GOAL_EVENTS:
            is_home = team_id == match.home_team_id

            # Автогол засчитывается сопернику.
            if event_type == MatchEventType.OWN_GOAL:
                is_home = not is_home

            if is_home:
                match.home_score = (
                    match.home_score or 0
                ) + 1
            else:
                match.away_score = (
                    match.away_score or 0
                ) + 1

        if event_type in MatchService.RED_CARD_EVENTS:
            await MatchService._apply_red_card_disqualification(
                db,
                match,
                event,
            )

        await db.commit()
        await db.refresh(event)

        return event

    @staticmethod
    async def _apply_red_card_disqualification(
        db: AsyncSession,
        match: Match,
        event: MatchEvent,
    ) -> None:
        """
        Создаёт или продлевает дисквалификацию
        за красную либо вторую жёлтую карточку.
        """

        from app.models.tournament import Tournament
        from app.models.disqualification import Disqualification

        if not event.player_id:
            return

        tournament = await db.get(
            Tournament,
            match.tournament_id,
        )

        if tournament is None:
            return

        if not getattr(
            tournament,
            "auto_disqualifications",
            True,
        ):
            return

        suspension = tournament.red_suspension
        if suspension is None:
            suspension = 1

        if suspension <= 0:
            return

        if event.event_type == MatchEventType.SECOND_YELLOW_CARD:
            reason = "Вторая жёлтая карточка"
        else:
            reason = "Красная карточка"

        existing = await db.scalar(
            select(Disqualification)
            .where(
                Disqualification.player_id == event.player_id,
                Disqualification.tournament_id
                == match.tournament_id,
                Disqualification.matches_remaining > 0,
            )
            .with_for_update()
            .limit(1)
        )

        if existing is not None:
            existing.matches_total = (
                existing.matches_total or 0
            ) + suspension

            existing.matches_remaining = (
                existing.matches_remaining or 0
            ) + suspension

            existing.reason = (
                f"{existing.reason}; {reason}"
                if existing.reason
                else reason
            )
        else:
            db.add(
                Disqualification(
                    player_id=event.player_id,
                    team_id=event.team_id,
                    tournament_id=match.tournament_id,
                    reason=reason,
                    matches_total=suspension,
                    matches_remaining=suspension,
                    decision_date=date.today(),
                )
            )

    @staticmethod
    async def finish_match(
        db: AsyncSession,
        match_id: int,
    ):
        """
        Завершает матч и пересчитывает таблицу.

        Повторное завершение не выполняет
        пересчёт без необходимости.
        """

        match = await db.scalar(
            select(Match)
            .where(Match.id == match_id)
            .with_for_update()
        )

        if match is None:
            raise ValueError("Матч не найден")

        if match.status == MatchStatus.FINISHED:
            return

        match.status = MatchStatus.FINISHED

        await db.flush()

        from app.services.standings import StandingsService

        await StandingsService.recalculate_standings(
            db,
            match.tournament_id,
        )

        # Текущая реализация StandingsService
        # самостоятельно вызывает commit().
        # Дополнительный commit здесь не нужен.

    @staticmethod
    def generate_round_robin(
        team_ids: list[int],
    ) -> list[tuple[int, int]]:
        """
        Генерирует пары для одного круга турнира.

        Каждая команда встречается с каждой один раз.
        """

        if len(team_ids) != len(set(team_ids)):
            raise ValueError(
                "В списке присутствуют повторяющиеся команды"
            )

        if len(team_ids) < 2:
            return []

        teams = list(team_ids)

        if len(teams) % 2:
            teams.append(None)

        count = len(teams)
        pairs = []

        for round_index in range(count - 1):
            for index in range(count // 2):
                home = teams[index]
                away = teams[count - 1 - index]

                if home is None or away is None:
                    continue

                if round_index % 2:
                    home, away = away, home

                pairs.append((home, away))

            teams = (
                [teams[0], teams[-1]]
                + teams[1:-1]
            )

        return pairs


def self_required_player_events() -> set:
    """Типы событий, для которых обязателен игрок."""
    return {
        MatchEventType.GOAL,
        MatchEventType.PENALTY_GOAL,
        MatchEventType.OWN_GOAL,
        MatchEventType.YELLOW_CARD,
        MatchEventType.SECOND_YELLOW_CARD,
        MatchEventType.RED_CARD,
    }
