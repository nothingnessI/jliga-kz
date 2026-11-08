from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.tournament_standing import TournamentStanding
from app.models.team_player import TeamPlayer
from app.models.team import Team
from app.models.enums import MatchStatus, MatchEventType, TournamentFormat
from app.services.standings import StandingsService


class StatsRecalculator:
    @staticmethod
    async def recalculate_all(db: AsyncSession, tournament_id: int):
        """Главный метод: пересчитывает таблицу, статистику команд и игроков"""

        # 1. Пересчитываем турнирную таблицу (очки, забитые, пропущенные)
        await StandingsService.recalculate_standings(db, tournament_id)

        # 2. Обнуляем статистику игроков (TeamPlayer) в этом турнире
        stmt_players = select(TeamPlayer).where(TeamPlayer.tournament_id == tournament_id)
        team_players = (await db.execute(stmt_players)).scalars().all()
        for tp in team_players:
            tp.stats_goals = 0
            tp.stats_assists = 0
            tp.stats_yellow_cards = 0
            tp.stats_red_cards = 0
            tp.stats_games = 0  # Пока обнуляем, ниже посчитаем

        await db.flush()

        # 3. Получаем все завершенные матчи турнира
        stmt_matches = select(Match).where(
            Match.tournament_id == tournament_id,
            Match.status == MatchStatus.FINISHED
        )
        matches = (await db.execute(stmt_matches)).scalars().all()
        match_ids = [m.id for m in matches]

        if match_ids:
            # 4. Считаем статистику игроков на основе событий
            stmt_events = select(MatchEvent).where(MatchEvent.match_id.in_(match_ids))
            events = (await db.execute(stmt_events)).scalars().all()

            # Словарь для быстрого поиска TeamPlayer по (player_id, team_id)
            tp_dict = {(tp.player_id, tp.team_id): tp for tp in team_players}

            for e in events:
                tp = tp_dict.get((e.player_id, e.team_id))
                if not tp:
                    continue

                if e.event_type == MatchEventType.GOAL:
                    tp.stats_goals += 1
                elif e.event_type == MatchEventType.PENALTY_GOAL:
                    tp.stats_goals += 1
                elif e.event_type == MatchEventType.ASSIST:
                    tp.stats_assists += 1
                elif e.event_type == MatchEventType.YELLOW_CARD:
                    tp.stats_yellow_cards += 1
                elif e.event_type == MatchEventType.RED_CARD:
                    tp.stats_red_cards += 1

            # 5. Считаем игры (stats_games) для игроков
            # (Для простоты считаем, что если игрок есть в заявке и матч завершен — он сыграл.
            # Если нужна точная заявка на матч, нужно добавить таблицу MatchLineup, но пока так)
            for tp in team_players:
                # Проверяем, играла ли команда игрока в завершенных матчах
                team_matches_count = sum(
                    1 for m in matches if m.home_team_id == tp.team_id or m.away_team_id == tp.team_id)
                # В реальности надо смотреть, был ли он в протоколе. Пока ставим кол-во игр команды
                tp.stats_games = team_matches_count

        # 6. Обновляем глобальную статистику команд (Team)
        # Получаем все команды, участвующие в турнире
        stmt_teams = select(Team).where(Team.id.in_([tp.team_id for tp in team_players]))
        teams = (await db.execute(stmt_teams)).scalars().all()

        for team in teams:
            # Считаем по всем завершенным матчам команды ВО ВСЕХ ТУРНИРАХ
            all_team_matches = (await db.execute(select(Match).where(
                Match.status == MatchStatus.FINISHED,
                (Match.home_team_id == team.id) | (Match.away_team_id == team.id)
            ))).scalars().all()

            games = len(all_team_matches)
            wins = 0
            goals = 0

            for m in all_team_matches:
                if m.home_team_id == team.id:
                    goals += (m.home_score or 0)
                    if (m.home_score or 0) > (m.away_score or 0): wins += 1
                else:
                    goals += (m.away_score or 0)
                    if (m.away_score or 0) > (m.home_score or 0): wins += 1

            team.stats_games = games
            team.stats_wins = wins
            team.stats_goals = goals
            # stats_tournaments можно считать отдельно, но не будем перегружать

        await db.commit()