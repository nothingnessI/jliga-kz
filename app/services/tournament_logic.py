from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.models.match import Match
from app.models.tournament import Tournament
from app.models.tournament_standing import TournamentStanding
from app.models.team_player import TeamPlayer
from app.models.match_event import MatchEvent
from app.models.enums import MatchStatus, MatchEventType


class TournamentLogicService:

    @staticmethod
    async def recalculate_standings(db: AsyncSession, tournament_id: int):
        """
        Полный пересчет турнирной таблицы.
        Вызывается после любого изменения матча (добавление события, изменение счета).
        """
        # 1. Получаем турнир, чтобы знать формат
        tournament = await db.get(Tournament, tournament_id)
        if not tournament:
            return

        # 2. Получаем все завершенные матчи этого турнира
        stmt = select(Match).where(
            Match.tournament_id == tournament_id,
            Match.status == MatchStatus.FINISHED
        )
        matches = (await db.execute(stmt)).scalars().all()

        # 3. Получаем все команды в турнире (из таблицы standings или tournament_teams)
        # Если таблицы еще нет, создаем пустые записи
        stmt_standings = select(TournamentStanding).where(TournamentStanding.tournament_id == tournament_id)
        standings = (await db.execute(stmt_standings)).scalars().all()

        # Словарь для быстрого доступа {team_id: standing_obj}
        standings_dict = {s.team_id: s for s in standings}

        # Сбрасываем все счетчики в 0 перед пересчетом
        for s in standings_dict.values():
            s.games_played = 0
            s.wins = 0
            s.draws = 0
            s.losses = 0
            s.goals_for = 0
            s.goals_against = 0
            s.points = 0
            s.yellow_cards = 0
            s.red_cards = 0
            s.clean_sheets = 0
            # progress (форма) мы обновим отдельно в pages.py

        # 4. Проходимся по каждому матчу и начисляем очки
        for m in matches:
            # Пропускаем матчи плей-офф, если таблица только для группового этапа
            # (если у вас общая таблица для всего кубка, то убирать этот if не нужно)
            if m.stage == 'PLAYOFF' and tournament.division != 'CUP_TABLE':
                continue

            home = standings_dict.get(m.home_team_id)
            away = standings_dict.get(m.away_team_id)

            if not home or not away:
                continue

            # Игры
            home.games_played += 1
            away.games_played += 1

            # Мячи
            home.goals_for += m.home_score
            home.goals_against += m.away_score
            away.goals_for += m.away_score
            away.goals_against += m.home_score

            # "Сухие" матчи (Clean Sheets)
            if m.away_score == 0: home.clean_sheets += 1
            if m.home_score == 0: away.clean_sheets += 1

            # Очки и исходы
            if m.home_score > m.away_score:
                home.wins += 1
                home.points += 3
                away.losses += 1
            elif m.home_score < m.away_score:
                away.wins += 1
                away.points += 3
                home.losses += 1
            else:
                # Ничья в основное время
                home.draws += 1
                away.draws += 1
                home.points += 1
                away.points += 1

                # Если есть серия пенальти, победитель по пенальти может получать +1 очко (по регламенту)
                # Если по регламенту пенальти не дают очков в таблицу, эту часть можно убрать
                if m.home_penalty is not None and m.away_penalty is not None:
                    if m.home_penalty > m.away_penalty:
                        home.points += 1  # Бонус за победу по пенальти
                    elif m.away_penalty > m.home_penalty:
                        away.points += 1

        # 5. Считаем карточки (из таблицы событий)
        for team_id, standing in standings_dict.items():
            stmt_yellow = select(MatchEvent).where(
                MatchEvent.match_id.in_([m.id for m in matches]),
                MatchEvent.team_id == team_id,
                MatchEvent.event_type == MatchEventType.YELLOW_CARD
            )
            stmt_red = select(MatchEvent).where(
                MatchEvent.match_id.in_([m.id for m in matches]),
                MatchEvent.team_id == team_id,
                MatchEvent.event_type == MatchEventType.RED_CARD
            )
            standing.yellow_cards = len((await db.execute(stmt_yellow)).scalars().all())
            standing.red_cards = len((await db.execute(stmt_red)).scalars().all())

        await db.commit()

    @staticmethod
    async def update_match_score_from_events(db: AsyncSession, match_id: int):
        """
        Пересчитывает счет матча на основе добавленных событий (голов).
        """
        match = await db.get(Match, match_id)
        if not match:
            return

        events = (await db.execute(
            select(MatchEvent).where(MatchEvent.match_id == match_id)
        )).scalars().all()

        home_goals = sum(1 for e in events if e.team_id == match.home_team_id and e.event_type in [MatchEventType.GOAL,
                                                                                                   MatchEventType.PENALTY_GOAL])
        away_goals = sum(1 for e in events if e.team_id == match.away_team_id and e.event_type in [MatchEventType.GOAL,
                                                                                                   MatchEventType.PENALTY_GOAL])

        match.home_score = home_goals
        match.away_score = away_goals

        await db.commit()

        # Автоматически пересчитываем таблицу после обновления счета
        await TournamentLogicService.recalculate_standings(db, match.tournament_id)