
import logging
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MatchEventType, MatchStatus
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.team import Team
from app.models.team_player import TeamPlayer
from app.models.tournament_team import TournamentTeam
from app.services.standings import StandingsService


logger = logging.getLogger(__name__)


class StatsRecalculator:
    """
    Полный пересчёт статистики турнира.

    Пересчитывает:
    - турнирную таблицу;
    - голы, передачи и карточки игроков;
    - количество сыгранных матчей;
    - глобальную статистику команд.

    Не вызывает commit().
    Транзакцией управляет вызывающий код.
    """

    @staticmethod
    async def recalculate_all(
        db: AsyncSession,
        tournament_id: int,
    ) -> None:
        """Пересчитывает статистику турнира."""

        logger.info(
            "Начат пересчёт статистики турнира %s",
            tournament_id,
        )

        # 1. Турнирная таблица.
        await StandingsService.recalculate_standings(
            db,
            tournament_id,
        )

        # 2. Составы команд в турнире.
        team_players = (
            await db.scalars(
                select(TeamPlayer)
                .where(
                    TeamPlayer.tournament_id == tournament_id
                )
                .with_for_update()
            )
        ).all()

        # Обнуляем вычисляемую статистику.
        for team_player in team_players:
            team_player.stats_goals = 0
            team_player.stats_assists = 0
            team_player.stats_yellow_cards = 0
            team_player.stats_red_cards = 0

            # Без протокола участия нельзя достоверно
            # определить число сыгранных матчей.
            team_player.stats_games = 0

        # Быстрый поиск по игроку и команде.
        roster_by_player_team = {
            (tp.player_id, tp.team_id): tp
            for tp in team_players
        }

        # 3. Завершённые матчи турнира.
        matches = (
            await db.scalars(
                select(Match).where(
                    Match.tournament_id == tournament_id,
                    Match.status == MatchStatus.FINISHED,
                )
            )
        ).all()

        match_by_id = {
            match.id: match
            for match in matches
        }

        if match_by_id:
            events = (
                await db.scalars(
                    select(MatchEvent).where(
                        MatchEvent.match_id.in_(
                            match_by_id.keys()
                        )
                    )
                )
            ).all()

            # 4. Пересчитываем индивидуальные показатели.
            for event in events:
                if event.player_id is None:
                    continue

                match = match_by_id.get(
                    event.match_id
                )

                if match is None:
                    continue

                if event.team_id not in (
                    match.home_team_id,
                    match.away_team_id,
                ):
                    logger.warning(
                        "Некорректная команда %s "
                        "у события %s",
                        event.team_id,
                        event.id,
                    )
                    continue

                team_player = roster_by_player_team.get(
                    (event.player_id, event.team_id)
                )

                if team_player is None:
                    logger.warning(
                        "Игрок %s не найден в составе "
                        "команды %s турнира %s",
                        event.player_id,
                        event.team_id,
                        tournament_id,
                    )
                    continue

                if event.event_type in (
                    MatchEventType.GOAL,
                    MatchEventType.PENALTY_GOAL,
                ):
                    team_player.stats_goals += 1

                elif event.event_type == (
                    MatchEventType.ASSIST
                ):
                    team_player.stats_assists += 1

                elif event.event_type == (
                    MatchEventType.YELLOW_CARD
                ):
                    team_player.stats_yellow_cards += 1

                elif event.event_type == (
                    MatchEventType.SECOND_YELLOW_CARD
                ):
                    team_player.stats_yellow_cards += 1
                    team_player.stats_red_cards += 1

                elif event.event_type == (
                    MatchEventType.RED_CARD
                ):
                    team_player.stats_red_cards += 1

        await db.flush()

        # 5. Получаем команды турнира независимо
        # от того, есть ли у них игроки в TeamPlayer.
        tournament_team_ids = set(
            await db.scalars(
                select(TournamentTeam.team_id).where(
                    TournamentTeam.tournament_id
                    == tournament_id
                )
            )
        )

        if not tournament_team_ids:
            logger.info(
                "В турнире %s нет зарегистрированных команд",
                tournament_id,
            )
            return

        # 6. Все завершённые матчи этих команд
        # во всех турнирах.
        all_matches = (
            await db.scalars(
                select(Match).where(
                    Match.status == MatchStatus.FINISHED,
                    (
                        Match.home_team_id.in_(
                            tournament_team_ids
                        )
                        |
                        Match.away_team_id.in_(
                            tournament_team_ids
                        )
                    ),
                )
            )
        ).all()

        team_stats = defaultdict(
            lambda: {
                "games": 0,
                "wins": 0,
                "goals": 0,
            }
        )

        for match in all_matches:
            home_id = match.home_team_id
            away_id = match.away_team_id

            home_goals = match.home_score or 0
            away_goals = match.away_score or 0

            if home_id in tournament_team_ids:
                team_stats[home_id]["games"] += 1
                team_stats[home_id]["goals"] += home_goals

                if home_goals > away_goals:
                    team_stats[home_id]["wins"] += 1

            if away_id in tournament_team_ids:
                team_stats[away_id]["games"] += 1
                team_stats[away_id]["goals"] += away_goals

                if away_goals > home_goals:
                    team_stats[away_id]["wins"] += 1

        # 7. Обновляем глобальные показатели команд.
        teams = (
            await db.scalars(
                select(Team)
                .where(
                    Team.id.in_(tournament_team_ids)
                )
                .with_for_update()
            )
        ).all()

        for team in teams:
            stats = team_stats[team.id]

            team.stats_games = stats["games"]
            team.stats_wins = stats["wins"]
            team.stats_goals = stats["goals"]

        await db.flush()

        logger.info(
            "Пересчёт статистики турнира %s завершён: "
            "%s матчей, %s игроков, %s команд",
            tournament_id,
            len(matches),
            len(team_players),
            len(teams),
        )
