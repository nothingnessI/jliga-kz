from sqlalchemy.ext.asyncio import AsyncSession
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.enums import MatchEventType, MatchStatus


class MatchService:
    @staticmethod
    async def add_event_and_update_score(db: AsyncSession, match_id: int, event_data: dict) -> MatchEvent:
        """Добавляет событие и автоматически меняет счет, если это гол."""
        match = await db.get(Match, match_id)
        if not match:
            raise ValueError("Матч не найден")
        if match.status == MatchStatus.FINISHED:
            raise ValueError("Нельзя добавить событие в завершенный матч")

        event = MatchEvent(**event_data, match_id=match_id)
        db.add(event)

        # Логика изменения счета
        if event.event_type in (MatchEventType.GOAL, MatchEventType.PENALTY_GOAL, MatchEventType.OWN_GOAL):
            if event.event_type == MatchEventType.OWN_GOAL:
                # Автогол идет в счет соперника
                if event.team_id == match.home_team_id:
                    match.away_score = (match.away_score or 0) + 1
                else:
                    match.home_score = (match.home_score or 0) + 1
            else:
                if event.team_id == match.home_team_id:
                    match.home_score = (match.home_score or 0) + 1
                else:
                    match.away_score = (match.away_score or 0) + 1

        await db.commit()
        await db.refresh(event)
        return event


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