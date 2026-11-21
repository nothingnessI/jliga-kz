from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.enums import MatchEventType, MatchStatus


class MatchService:
    @staticmethod
    async def add_event_and_update_score(
        db: AsyncSession, match_id: int, event_data: dict
    ) -> MatchEvent:
        """Добавляет событие и автоматически меняет счет, если это гол."""
        match = await db.get(Match, match_id)
        if not match:
            raise ValueError("Матч не найден")
        if match.status == MatchStatus.FINISHED:
            raise ValueError("Нельзя добавить событие в завершенный матч")

        event_data.pop("match_id", None)
        event_type = event_data.get("event_type")
        player_id = event_data.get("player_id")

        # --- Логика ЖК / второй ЖК ---
        # 1. Автоконвертация: если ставят ЖК, а у игрока ЖК в матче уже есть → вторая ЖК
        if event_type == MatchEventType.YELLOW_CARD and player_id:
            existing_yellow = await db.scalar(
                select(MatchEvent).where(
                    MatchEvent.match_id == match_id,
                    MatchEvent.player_id == player_id,
                    MatchEvent.event_type == MatchEventType.YELLOW_CARD,
                ).limit(1)
            )
            if existing_yellow:
                event_data["event_type"] = MatchEventType.SECOND_YELLOW_CARD
                event_type = MatchEventType.SECOND_YELLOW_CARD

        # 2. Валидация: нельзя добавить вторую ЖК без первой
        if event_type == MatchEventType.SECOND_YELLOW_CARD:
            if not player_id:
                raise ValueError("Вторая ЖК требует указания игрока")
            has_yellow = await db.scalar(
                select(MatchEvent).where(
                    MatchEvent.match_id == match_id,
                    MatchEvent.player_id == player_id,
                    MatchEvent.event_type == MatchEventType.YELLOW_CARD,
                ).limit(1)
            )
            if not has_yellow:
                raise ValueError(
                    "Нельзя добавить вторую ЖК: у игрока нет первой ЖК в этом матче"
                )

        event = MatchEvent(**event_data, match_id=match_id)
        db.add(event)

        # Логика изменения счета
        if event.event_type in (
            MatchEventType.GOAL,
            MatchEventType.PENALTY_GOAL,
            MatchEventType.OWN_GOAL,
        ):
            if event.event_type == MatchEventType.OWN_GOAL:
                if event.team_id == match.home_team_id:
                    match.away_score = (match.away_score or 0) + 1
                else:
                    match.home_score = (match.home_score or 0) + 1
            else:
                if event.team_id == match.home_team_id:
                    match.home_score = (match.home_score or 0) + 1
                else:
                    match.away_score = (match.away_score or 0) + 1

        # --- Дисквалификация за вторую ЖК / прямую КК ---
        if event.event_type in (
            MatchEventType.SECOND_YELLOW_CARD,
            MatchEventType.RED_CARD,
        ):
            await MatchService._apply_red_card_disqualification(db, match, event)

        await db.commit()
        await db.refresh(event)
        return event

    @staticmethod
    async def _apply_red_card_disqualification(
        db: AsyncSession, match: Match, event: MatchEvent
    ) -> None:
        """Создаёт/продлевает дисквалификацию при КК или второй ЖК."""
        from app.models.tournament import Tournament
        from app.models.disqualification import Disqualification

        if not event.player_id:
            return

        tournament = await db.get(Tournament, match.tournament_id)
        if not tournament or not getattr(tournament, "auto_disqualifications", True):
            return

        suspension = tournament.red_suspension or 1

        if event.event_type == MatchEventType.SECOND_YELLOW_CARD:
            reason = "Вторая жёлтая карточка"
        else:
            reason = "Красная карточка"

        # Если есть активная дисквалификация — продлеваем
        existing = await db.scalar(
            select(Disqualification).where(
                Disqualification.player_id == event.player_id,
                Disqualification.tournament_id == match.tournament_id,
                Disqualification.matches_remaining > 0,
            ).limit(1)
        )

        if existing:
            existing.matches_total = (existing.matches_total or 0) + suspension
            existing.matches_remaining = (existing.matches_remaining or 0) + suspension
            existing.reason = f"{existing.reason}; {reason}"
        else:
            db.add(Disqualification(
                player_id=event.player_id,
                team_id=event.team_id,
                tournament_id=match.tournament_id,
                reason=reason,
                matches_total=suspension,
                matches_remaining=suspension,
                decision_date=date.today(),
            ))

    @staticmethod
    async def finish_match(db: AsyncSession, match_id: int):
        """Завершает матч и запускает пересчет таблицы."""
        match = await db.get(Match, match_id)
        if not match:
            raise ValueError("Матч не найден")

        match.status = MatchStatus.FINISHED
        await db.commit()

        # Импорт здесь, чтобы избежать циклической зависимости
        from app.services.standings import StandingsService
        await StandingsService.recalculate_standings(db, match.tournament_id)

    @staticmethod
    def generate_round_robin(team_ids: list[int]) -> list[tuple[int, int]]:
        """Генерирует пары команд для кругового турнира (каждый с каждым)."""
        teams = list(team_ids)
        if len(teams) % 2 != 0:
            teams.append(None)  # Добавляем "пустую" команду, если число нечетное

        n = len(teams)
        pairs = []

        for i in range(n - 1):
            for j in range(n // 2):
                home = teams[j]
                away = teams[n - 1 - j]
                if home is not None and away is not None:
                    # Чередуем домашние и гостевые матчи
                    if i % 2 == 0:
                        pairs.append((home, away))
                    else:
                        pairs.append((away, home))
            # Ротация команд (кроме первой)
            teams = [teams[0]] + [teams[-1]] + teams[1:-1]

        return pairs