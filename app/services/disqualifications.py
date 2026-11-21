
import logging
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.disqualification import Disqualification
from app.models.enums import MatchEventType, MatchStatus
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.tournament import Tournament


logger = logging.getLogger(__name__)


class DisqualificationService:
    """
    Управление автоматическими дисквалификациями.

    Методы не вызывают commit().
    Сохранение транзакции выполняет вызывающий код.
    """

    @staticmethod
    async def process_match_finished(
        db: AsyncSession,
        match: Match,
    ) -> int:
        """
        Списывает один матч с активных дисквалификаций
        игроков команд, участвовавших в матче.

        ВАЖНО:
        Вызывать только один раз при переходе матча
        из незавершённого состояния в FINISHED.

        Возвращает количество обновлённых записей.
        """

        if match.status != MatchStatus.FINISHED:
            return 0

        team_ids = {
            match.home_team_id,
            match.away_team_id,
        }

        team_ids.discard(None)

        if not team_ids:
            return 0

        stmt = (
            select(Disqualification)
            .where(
                Disqualification.tournament_id
                == match.tournament_id,
                Disqualification.team_id.in_(team_ids),
                Disqualification.matches_remaining > 0,
            )
            .with_for_update()
        )

        disqualifications = (
            await db.scalars(stmt)
        ).all()

        updated = 0

        for disqualification in disqualifications:
            disqualification.matches_remaining -= 1
            updated += 1

            logger.info(
                "Дисквалификация игрока %s: осталось %s",
                disqualification.player_id,
                disqualification.matches_remaining,
            )

        await db.flush()

        return updated

    @staticmethod
    async def auto_suspend_by_cards(
        db: AsyncSession,
        match: Match,
    ) -> int:
        """
        Назначает автоматические дисквалификации
        на основании карточек завершённого матча.

        Поддерживаются:
        - прямая красная карточка;
        - вторая жёлтая карточка;
        - накопление жёлтых карточек.

        Не вызывает commit().

        ВАЖНО:
        Если красные карточки уже обрабатываются
        через MatchService._apply_red_card_disqualification,
        повторно назначать наказание здесь нельзя.
        """

        if match.status != MatchStatus.FINISHED:
            return 0

        tournament = await db.get(
            Tournament,
            match.tournament_id,
        )

        if tournament is None:
            logger.warning(
                "Турнир %s не найден",
                match.tournament_id,
            )
            return 0

        if not getattr(
            tournament,
            "auto_disqualifications",
            False,
        ):
            return 0

        yellow_limit = (
            tournament.yellow_cards_limit
            if tournament.yellow_cards_limit is not None
            else 4
        )

        yellow_suspension = (
            tournament.yellow_suspension
            if tournament.yellow_suspension is not None
            else 1
        )

        red_suspension = (
            tournament.red_suspension
            if tournament.red_suspension is not None
            else 1
        )

        if yellow_limit <= 0:
            logger.warning(
                "Некорректный лимит жёлтых карточек "
                "в турнире %s",
                tournament.id,
            )
            return 0

        events = (
            await db.scalars(
                select(MatchEvent)
                .where(
                    MatchEvent.match_id == match.id,
                    MatchEvent.event_type.in_(
                        [
                            MatchEventType.YELLOW_CARD,
                            MatchEventType.SECOND_YELLOW_CARD,
                            MatchEventType.RED_CARD,
                        ]
                    ),
                )
                .order_by(MatchEvent.id)
            )
        ).all()

        if not events:
            return 0

        created = 0
        processed_players = set()

        for event in events:
            if event.player_id is None:
                continue

            player_id = event.player_id

            if event.event_type in (
                MatchEventType.RED_CARD,
                MatchEventType.SECOND_YELLOW_CARD,
            ):
                # Красные карточки уже обрабатываются
                # в MatchService при создании события.
                # Здесь не создаём вторую дисквалификацию.
                continue

            if event.event_type != MatchEventType.YELLOW_CARD:
                continue

            # Одного игрока проверяем только один раз
            # за обработку матча.
            if player_id in processed_players:
                continue

            processed_players.add(player_id)

            yellow_count = await db.scalar(
                select(func.count(MatchEvent.id))
                .select_from(MatchEvent)
                .join(
                    Match,
                    MatchEvent.match_id == Match.id,
                )
                .where(
                    MatchEvent.player_id == player_id,
                    Match.tournament_id == match.tournament_id,
                    Match.status == MatchStatus.FINISHED,
                    MatchEvent.event_type.in_(
                        [
                            MatchEventType.YELLOW_CARD,
                            MatchEventType.SECOND_YELLOW_CARD,
                        ]
                    ),
                )
            )

            yellow_count = yellow_count or 0

            if yellow_count == 0:
                continue

            if yellow_count % yellow_limit != 0:
                continue

            if yellow_suspension <= 0:
                continue

            existing = await db.scalar(
                select(Disqualification)
                .where(
                    Disqualification.player_id == player_id,
                    Disqualification.tournament_id
                    == match.tournament_id,
                    Disqualification.matches_remaining > 0,
                )
                .with_for_update()
                .limit(1)
            )

            if existing is not None:
                # Не создаём параллельное активное наказание.
                logger.info(
                    "У игрока %s уже есть активная "
                    "дисквалификация",
                    player_id,
                )
                continue

            disqualification = Disqualification(
                tournament_id=match.tournament_id,
                team_id=event.team_id,
                player_id=player_id,
                matches_total=yellow_suspension,
                matches_remaining=yellow_suspension,
                reason=(
                    f"Накопление {yellow_limit} "
                    "жёлтых карточек"
                ),
                decision_date=date.today(),
            )

            db.add(disqualification)
            created += 1

            logger.info(
                "Игрок %s дисквалифицирован на %s матч(а)",
                player_id,
                yellow_suspension,
            )

        await db.flush()

        return created
