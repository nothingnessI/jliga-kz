from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.tournament import Tournament
from app.models.match import Match
from app.models.tournament_standing import TournamentStanding
from app.models.tournament_team import TournamentTeam
from app.models.match_event import MatchEvent
from app.models.enums import MatchStatus, MatchEventType


class StandingsService:
    @staticmethod
    async def recalculate_standings(db: AsyncSession, tournament_id: int):
        tournament = await db.get(Tournament, tournament_id)
        if not tournament:
            return

        # =====================================================================
        # 1. ПОЛУЧАЕМ КОМАНДЫ И ИНИЦИАЛИЗИРУЕМ/ОБНОВЛЯЕМ ТАБЛИЦУ
        # =====================================================================
        stmt_teams = select(TournamentTeam).where(TournamentTeam.tournament_id == tournament_id)
        tournament_teams = (await db.execute(stmt_teams)).scalars().all()

        stmt_standings = select(TournamentStanding).where(TournamentStanding.tournament_id == tournament_id)
        standings = (await db.execute(stmt_standings)).scalars().all()
        standings_dict = {s.team_id: s for s in standings}

        # Создаем строки для команд, которых еще нет в таблице
        for tt in tournament_teams:
            if tt.team_id not in standings_dict:
                new_s = TournamentStanding(
                    tournament_id=tournament_id,
                    team_id=tt.team_id,
                    group_name=tt.group_name,
                    games_played=0, wins=0, draws=0, losses=0,
                    goals_for=0, goals_against=0, points=0,
                    yellow_cards=0, red_cards=0, clean_sheets=0
                )
                db.add(new_s)
                standings_dict[tt.team_id] = new_s
        await db.flush()

        # =====================================================================
        # 2. СБРАСЫВАЕМ СЧЕТЧИКИ ПЕРЕД ПЕРЕСЧЕТОМ
        # =====================================================================
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

        # =====================================================================
        # 3. СЧИТАЕМ ОЧКИ И ГОЛЫ ПО ЗАВЕРШЕННЫМ МАТЧАМ (ПО РЕГЛАМЕНТУ)
        # =====================================================================
        stmt_matches = select(Match).where(
            Match.tournament_id == tournament_id,
            Match.status == MatchStatus.FINISHED
        )
        matches = (await db.execute(stmt_matches)).scalars().all()

        # Берем правила из регламента турнира (или дефолтные)
        p_win = tournament.points_win if tournament.points_win is not None else 3
        p_draw = tournament.points_draw if tournament.points_draw is not None else 1
        p_loss = tournament.points_loss if tournament.points_loss is not None else 0

        for m in matches:
            home = standings_dict.get(m.home_team_id)
            away = standings_dict.get(m.away_team_id)
            if not home or not away: continue

            home.games_played += 1
            away.games_played += 1

            # Логика технического поражения
            if m.tech_loss:
                # Обычно ТП = 3:0
                h_goals = 3 if (m.home_score or 0) > (m.away_score or 0) else 0
                a_goals = 3 if (m.away_score or 0) > (m.home_score or 0) else 0

                # Если в регламенте указано не учитывать голы при ТП
                if not tournament.tech_loss_goals:
                    h_goals = 0
                    a_goals = 0
            else:
                h_goals = m.home_score or 0
                a_goals = m.away_score or 0

            # Начисляем забитые/пропущенные
            home.goals_for += h_goals
            home.goals_against += a_goals
            away.goals_for += a_goals
            away.goals_against += h_goals

            # "Сухие" матчи (clean sheets)
            if a_goals == 0: home.clean_sheets += 1
            if h_goals == 0: away.clean_sheets += 1

            # Начисление очков по регламенту
            if h_goals > a_goals:
                home.wins += 1
                home.points += p_win
                away.losses += 1
                away.points += p_loss
            elif h_goals < a_goals:
                away.wins += 1
                away.points += p_win
                home.losses += 1
                home.points += p_loss
            else:
                home.draws += 1
                away.draws += 1
                home.points += p_draw
                away.points += p_draw

                # Бонус за победу по пенальти (если они есть в матче)
                if m.home_penalty is not None and m.away_penalty is not None:
                    if m.home_penalty > m.away_penalty:
                        home.points += 1
                    elif m.away_penalty > m.home_penalty:
                        away.points += 1

        # =====================================================================
        # 4. СЧИТАЕМ КАРТОЧКИ (ОПТИМИЗИРОВАННЫЙ ЗАПРОС БЕЗ N+1)
        # =====================================================================
        if matches:
            match_ids = [m.id for m in matches]

            # Запрашиваем все события типа "карточка" для всех матчей турнира
            stmt_cards = select(MatchEvent).where(
                MatchEvent.match_id.in_(match_ids),
                MatchEvent.event_type.in_([MatchEventType.YELLOW_CARD, MatchEventType.RED_CARD])
            )
            card_events = (await db.execute(stmt_cards)).scalars().all()

            # Распределяем по командам
            for event in card_events:
                team_standing = standings_dict.get(event.team_id)
                if team_standing:
                    if event.event_type == MatchEventType.YELLOW_CARD:
                        team_standing.yellow_cards += 1
                    elif event.event_type == MatchEventType.RED_CARD:
                        team_standing.red_cards += 1

        # Сохраняем изменения в базу
        await db.commit()

    # =========================================================================
    # ХЕЛПЕР: СОРТИРОВКА ТАБЛИЦЫ С УЧЕТОМ ЛИЧНЫХ ВСТРЕЧ (tie_breakers)
    # =========================================================================
    @staticmethod
    def sort_standings(standings: list, tie_breakers: list | None) -> list:
        """
        Сортирует таблицу на основе правил из регламента (tie_breakers).
        Если tie_breakers нет, сортирует по стандарту: Очки -> Разница -> Забитые.
        """
        if not tie_breakers:
            return sorted(standings, key=lambda x: (-x.points, -(x.goals_for - x.goals_against), -x.goals_for))

        # Базовая сортировка всегда по очкам (по убыванию)
        # Для полной реализации личных встреч нужен сложный контекст матчей,
        # поэтому здесь реализован базовый вариант, который можно расширять.

        return sorted(
            standings,
            key=lambda x: (
                -x.points,
                -(x.goals_for - x.goals_against),
                -x.goals_for,
                -x.wins
            )
        )