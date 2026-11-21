
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import MatchEventType, MatchStatus
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.tournament import Tournament
from app.models.tournament_standing import TournamentStanding
from app.models.tournament_team import TournamentTeam


class StandingsService:
    """Расчёт и сортировка турнирных таблиц."""

    @staticmethod
    async def recalculate_standings(
        db: AsyncSession,
        tournament_id: int,
    ) -> list:
        """
        Полностью пересчитывает турнирную таблицу.

        ВАЖНО:
        Метод не вызывает commit().
        Сохранение транзакции выполняет вызывающий код.
        """

        tournament = await db.get(
            Tournament,
            tournament_id,
        )

        if tournament is None:
            return []

        tournament_teams = (
            await db.scalars(
                select(TournamentTeam).where(
                    TournamentTeam.tournament_id
                    == tournament_id
                )
            )
        ).all()

        standings = (
            await db.scalars(
                select(TournamentStanding)
                .where(
                    TournamentStanding.tournament_id
                    == tournament_id
                )
                .with_for_update()
            )
        ).all()

        standings_dict = {
            standing.team_id: standing
            for standing in standings
        }

        # Создаём недостающие строки.
        for tournament_team in tournament_teams:
            standing = standings_dict.get(
                tournament_team.team_id
            )

            if standing is None:
                standing = TournamentStanding(
                    tournament_id=tournament_id,
                    team_id=tournament_team.team_id,
                    group_name=tournament_team.group_name,
                    games_played=0,
                    wins=0,
                    draws=0,
                    losses=0,
                    goals_for=0,
                    goals_against=0,
                    points=0,
                    yellow_cards=0,
                    red_cards=0,
                    clean_sheets=0,
                )

                db.add(standing)
                standings_dict[
                    tournament_team.team_id
                ] = standing

            else:
                standing.group_name = (
                    tournament_team.group_name
                )

        await db.flush()

        matches = (
            await db.scalars(
                select(Match).where(
                    Match.tournament_id == tournament_id,
                    Match.status == MatchStatus.FINISHED,
                )
            )
        ).all()

        # Сбрасываем вычисляемые показатели.
        for standing in standings_dict.values():
            standing.games_played = 0
            standing.wins = 0
            standing.draws = 0
            standing.losses = 0
            standing.goals_for = 0
            standing.goals_against = 0
            standing.points = 0
            standing.yellow_cards = 0
            standing.red_cards = 0
            standing.clean_sheets = 0

        points_win = (
            tournament.points_win
            if tournament.points_win is not None
            else 3
        )
        points_draw = (
            tournament.points_draw
            if tournament.points_draw is not None
            else 1
        )
        points_loss = (
            tournament.points_loss
            if tournament.points_loss is not None
            else 0
        )

        # Рассчитываем результаты завершённых матчей.
        for match in matches:
            home = standings_dict.get(
                match.home_team_id
            )
            away = standings_dict.get(
                match.away_team_id
            )

            if home is None or away is None:
                continue

            home_goals, away_goals = (
                StandingsService._match_score(
                    match,
                    tournament,
                )
            )

            home.games_played += 1
            away.games_played += 1

            home.goals_for += home_goals
            home.goals_against += away_goals

            away.goals_for += away_goals
            away.goals_against += home_goals

            if away_goals == 0:
                home.clean_sheets += 1

            if home_goals == 0:
                away.clean_sheets += 1

            if home_goals > away_goals:
                home.wins += 1
                away.losses += 1

                home.points += points_win
                away.points += points_loss

            elif away_goals > home_goals:
                away.wins += 1
                home.losses += 1

                away.points += points_win
                home.points += points_loss

            else:
                home.draws += 1
                away.draws += 1

                home.points += points_draw
                away.points += points_draw

                # Сохраняем существующее правило проекта:
                # победитель серии пенальти получает
                # дополнительное очко.
                if (
                    match.home_penalty is not None
                    and match.away_penalty is not None
                ):
                    if (
                        match.home_penalty
                        > match.away_penalty
                    ):
                        home.points += 1
                    elif (
                        match.away_penalty
                        > match.home_penalty
                    ):
                        away.points += 1

        # Считаем карточки только завершённых матчей.
        if matches:
            match_ids = [
                match.id
                for match in matches
            ]

            events = (
                await db.scalars(
                    select(MatchEvent).where(
                        MatchEvent.match_id.in_(
                            match_ids
                        ),
                        MatchEvent.event_type.in_(
                            [
                                MatchEventType.YELLOW_CARD,
                                MatchEventType.SECOND_YELLOW_CARD,
                                MatchEventType.RED_CARD,
                            ]
                        ),
                    )
                )
            ).all()

            for event in events:
                standing = standings_dict.get(
                    event.team_id
                )

                if standing is None:
                    continue

                if event.event_type == (
                    MatchEventType.YELLOW_CARD
                ):
                    standing.yellow_cards += 1

                elif event.event_type == (
                    MatchEventType.SECOND_YELLOW_CARD
                ):
                    standing.yellow_cards += 1
                    standing.red_cards += 1

                elif event.event_type == (
                    MatchEventType.RED_CARD
                ):
                    standing.red_cards += 1

        await db.flush()

        return StandingsService.sort_standings(
            list(standings_dict.values()),
            matches,
            tournament,
        )

    @staticmethod
    def _match_score(
        match: Match,
        tournament: Tournament,
    ) -> tuple[int, int]:
        """
        Возвращает результат матча для таблицы.

        При техническом поражении сохраняет
        записанный результат, если регламент
        разрешает учитывать технические голы.
        """

        home_goals = match.home_score or 0
        away_goals = match.away_score or 0

        if (
            getattr(match, "tech_loss", False)
            and not getattr(
                tournament,
                "tech_loss_goals",
                True,
            )
        ):
            return 0, 0

        return home_goals, away_goals

    @staticmethod
    def sort_standings(
        standings: list,
        matches: list,
        tournament,
    ) -> list:
        """
        Сортировка таблицы по группам.

        Основные критерии:
        1. Очки
        2. Разница голов
        3. Забитые голы
        4. Победы

        При h2h_always сначала применяются
        показатели личных встреч для команд
        с равным количеством очков.
        """

        groups = defaultdict(list)

        for standing in standings:
            group_name = (
                standing.group_name
                or "Общая таблица"
            )
            groups[group_name].append(standing)

        use_h2h = getattr(
            tournament,
            "h2h_always",
            False,
        )

        result = []

        for group_name, teams in groups.items():
            teams.sort(
                key=StandingsService._general_sort_key,
                reverse=True,
            )

            if use_h2h:
                index = 0

                while index < len(teams):
                    end = index + 1

                    while (
                        end < len(teams)
                        and teams[end].points
                        == teams[index].points
                    ):
                        end += 1

                    if end - index > 1:
                        tied_teams = teams[index:end]

                        tied_ids = {
                            team.team_id
                            for team in tied_teams
                        }

                        h2h_matches = [
                            match
                            for match in matches
                            if (
                                match.home_team_id
                                in tied_ids
                                and match.away_team_id
                                in tied_ids
                            )
                        ]

                        if h2h_matches:
                            h2h_stats = (
                                StandingsService._calculate_h2h(
                                    tied_ids,
                                    h2h_matches,
                                    tournament,
                                )
                            )

                            tied_teams.sort(
                                key=lambda team: (
                                    h2h_stats[
                                        team.team_id
                                    ]["points"],
                                    h2h_stats[
                                        team.team_id
                                    ]["goals_for"]
                                    - h2h_stats[
                                        team.team_id
                                    ]["goals_against"],
                                    h2h_stats[
                                        team.team_id
                                    ]["goals_for"],
                                    *StandingsService
                                    ._general_sort_key(
                                        team
                                    ),
                                ),
                                reverse=True,
                            )

                            teams[index:end] = tied_teams

                    index = end

            result.extend(teams)

        return result

    @staticmethod
    def _general_sort_key(standing) -> tuple:
        """Ключ обычной сортировки таблицы."""

        points = standing.points or 0
        goals_for = standing.goals_for or 0
        goals_against = standing.goals_against or 0
        wins = standing.wins or 0

        return (
            points,
            goals_for - goals_against,
            goals_for,
            wins,
        )

    @staticmethod
    def _calculate_h2h(
        team_ids: set[int],
        matches: list,
        tournament,
    ) -> dict:
        """Рассчитывает мини-таблицу личных встреч."""

        stats = {
            team_id: {
                "points": 0,
                "goals_for": 0,
                "goals_against": 0,
            }
            for team_id in team_ids
        }

        points_win = (
            tournament.points_win
            if tournament.points_win is not None
            else 3
        )
        points_draw = (
            tournament.points_draw
            if tournament.points_draw is not None
            else 1
        )
        points_loss = (
            tournament.points_loss
            if tournament.points_loss is not None
            else 0
        )

        for match in matches:
            home_id = match.home_team_id
            away_id = match.away_team_id

            home_goals, away_goals = (
                StandingsService._match_score(
                    match,
                    tournament,
                )
            )

            home = stats[home_id]
            away = stats[away_id]

            home["goals_for"] += home_goals
            home["goals_against"] += away_goals

            away["goals_for"] += away_goals
            away["goals_against"] += home_goals

            if home_goals > away_goals:
                home["points"] += points_win
                away["points"] += points_loss

            elif away_goals > home_goals:
                away["points"] += points_win
                home["points"] += points_loss

            else:
                home["points"] += points_draw
                away["points"] += points_draw

                if (
                    match.home_penalty is not None
                    and match.away_penalty is not None
                ):
                    if (
                        match.home_penalty
                        > match.away_penalty
                    ):
                        home["points"] += 1

                    elif (
                        match.away_penalty
                        > match.home_penalty
                    ):
                        away["points"] += 1

        return stats
