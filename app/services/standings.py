from sqlalchemy import select
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

        # 1. Получаем все команды турнира и создаем/обновляем таблицы
        stmt_teams = select(TournamentTeam).where(TournamentTeam.tournament_id == tournament_id)
        tournament_teams = (await db.execute(stmt_teams)).scalars().all()

        stmt_standings = select(TournamentStanding).where(TournamentStanding.tournament_id == tournament_id)
        standings = (await db.execute(stmt_standings)).scalars().all()
        standings_dict = {s.team_id: s for s in standings}

        for tt in tournament_teams:
            if tt.team_id not in standings_dict:
                new_s = TournamentStanding(
                    tournament_id=tournament_id, team_id=tt.team_id, group_name=tt.group_name,
                    games_played=0, wins=0, draws=0, losses=0, goals_for=0, goals_against=0, points=0,
                    yellow_cards=0, red_cards=0, clean_sheets=0
                )
                db.add(new_s)
                standings_dict[tt.team_id] = new_s
        await db.flush()

        # 2. Сбрасываем счетчики
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

        # 3. Считаем очки и голы по завершенным матчам
        stmt_matches = select(Match).where(
            Match.tournament_id == tournament_id,
            Match.status == MatchStatus.FINISHED
        )
        matches = (await db.execute(stmt_matches)).scalars().all()

        for m in matches:
            home = standings_dict.get(m.home_team_id)
            away = standings_dict.get(m.away_team_id)
            if not home or not away: continue

            home.games_played += 1
            away.games_played += 1

            # Если ТЕХНИЧЕСКОЕ ПОРАЖЕНИЕ, ставим фиксированный счет 3:0
            if m.tech_loss:
                hs = m.home_score or 0
                aws = m.away_score or 0
                h_goals = 3 if hs > aws else 0
                a_goals = 3 if aws > hs else 0
            else:
                h_goals = m.home_score or 0
                a_goals = m.away_score or 0


            home.goals_for += h_goals
            home.goals_against += a_goals
            away.goals_for += a_goals
            away.goals_against += h_goals

            if a_goals == 0: home.clean_sheets += 1
            if h_goals == 0: away.clean_sheets += 1

            # Начисление очков
            if h_goals > a_goals:
                home.wins += 1
                home.points += 3
                away.losses += 1
            elif h_goals < a_goals:
                away.wins += 1
                away.points += 3
                home.losses += 1
            else:
                home.draws += 1
                away.draws += 1
                home.points += 1
                away.points += 1

                # Бонус за победу по пенальти (если они есть в матче)
                if m.home_penalty is not None and m.away_penalty is not None:
                    if m.home_penalty > m.away_penalty:
                        home.points += 1
                    elif m.away_penalty > m.home_penalty:
                        away.points += 1

        # 4. Считаем карточки
        for s in standings_dict.values():
            yellow_stmt = select(MatchEvent).where(
                MatchEvent.match_id.in_([m.id for m in matches]),
                MatchEvent.team_id == s.team_id,
                MatchEvent.event_type == MatchEventType.YELLOW_CARD
            )
            s.yellow_cards = len((await db.execute(yellow_stmt)).scalars().all())

            red_stmt = select(MatchEvent).where(
                MatchEvent.match_id.in_([m.id for m in matches]),
                MatchEvent.team_id == s.team_id,
                MatchEvent.event_type == MatchEventType.RED_CARD
            )
            s.red_cards = len((await db.execute(red_stmt)).scalars().all())

        await db.commit()