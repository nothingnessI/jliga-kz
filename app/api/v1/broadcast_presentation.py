
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Response,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import get_db
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.team_player import TeamPlayer
from app.models.tournament_standing import TournamentStanding
from app.services.broadcast import (
    BroadcastService,
    state_response,
)


router = APIRouter(
    prefix="/broadcast",
    tags=["Broadcast Presentation"],
)


def enum_value(value):
    return (
        value.value
        if hasattr(value, "value")
        else value
    )


def player_name(player):
    if player is None:
        return "Игрок"

    return (
        f"{player.last_name} {player.first_name}"
    ).strip()


def team_info(team):
    if team is None:
        return {
            "id": None,
            "name": "Команда",
            "short_name": "TEAM",
            "logo_url": None,
            "coach_name": None,
            "players": [],
        }

    return {
        "id": team.id,
        "name": team.name,
        "short_name": team.name[:14],
        "logo_url": team.logo_url,
        "coach_name": team.coach_name,
        "players": [],
    }


@router.get(
    "/sessions/{session_id}/presentation"
)
async def presentation(
    session_id: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = (
        "no-store, no-cache, must-revalidate"
    )

    session = await BroadcastService.get_session(
        db,
        session_id,
    )

    match = await db.scalar(
        select(Match)
        .options(
            selectinload(Match.home_team),
            selectinload(Match.away_team),
            selectinload(Match.tournament),
        )
        .where(Match.id == session.match_id)
    )

    if match is None:
        raise HTTPException(
            status_code=404,
            detail="Матч не найден",
        )

    home = team_info(match.home_team)
    away = team_info(match.away_team)

    teams = {
        match.home_team_id: home,
        match.away_team_id: away,
    }

    # ========================================
    # ЗАЯВКИ
    # ========================================

    registrations = (
        await db.scalars(
            select(TeamPlayer)
            .where(
                TeamPlayer.tournament_id
                == match.tournament_id,
                TeamPlayer.team_id.in_([
                    match.home_team_id,
                    match.away_team_id,
                ]),
                TeamPlayer.is_active.is_(True),
            )
            .options(
                selectinload(TeamPlayer.player),
            )
            .order_by(
                TeamPlayer.team_id,
                TeamPlayer.jersey_number,
                TeamPlayer.player_id,
            )
        )
    ).all()

    for row in registrations:
        if row.player is None:
            continue

        current_team = teams.get(row.team_id)

        if current_team is None:
            continue

        current_team["players"].append({
            "id": row.player_id,
            "number": row.jersey_number,
            "name": player_name(row.player),
            "position": enum_value(
                row.player.position
            ),
            "captain": row.is_captain,
            "photo_url": row.player.photo_url,
        })

    # ========================================
    # СОБЫТИЯ
    # ========================================

    event_rows = (
        await db.scalars(
            select(MatchEvent)
            .where(MatchEvent.match_id == match.id)
            .options(
                selectinload(MatchEvent.player),
                selectinload(MatchEvent.team),
            )
            .order_by(
                MatchEvent.minute.desc(),
                MatchEvent.id.desc(),
            )
            .limit(150)
        )
    ).all()

    events = []

    for event in event_rows:
        events.append({
            "id": event.id,
            "minute": event.minute,
            "event_type": enum_value(
                event.event_type
            ),
            "player_name": player_name(
                event.player
            ),
            "team_id": event.team_id,
            "team_name": (
                event.team.name
                if event.team else "Команда"
            ),
            "comment": event.comment,
        })

    # ========================================
    # ТАБЛИЦА
    # ========================================

    standings_query = (
        select(TournamentStanding)
        .where(
            TournamentStanding.tournament_id
            == match.tournament_id
        )
        .options(
            selectinload(TournamentStanding.team),
        )
        .order_by(
            TournamentStanding.points.desc(),
            (
                TournamentStanding.goals_for
                - TournamentStanding.goals_against
            ).desc(),
            TournamentStanding.goals_for.desc(),
            TournamentStanding.team_id,
        )
        .limit(32)
    )

    if match.group_name:
        standings_query = standings_query.where(
            TournamentStanding.group_name
            == match.group_name
        )

    standings_rows = (
        await db.scalars(standings_query)
    ).all()

    standings = []

    for row in standings_rows:
        standings.append({
            "team_id": row.team_id,
            "name": (
                row.team.name
                if row.team else "Команда"
            ),
            "played": row.games_played,
            "wins": row.wins,
            "draws": row.draws,
            "losses": row.losses,
            "goals_for": row.goals_for,
            "goals_against": row.goals_against,
            "goal_difference": (
                row.goals_for
                - row.goals_against
            ),
            "points": row.points,
            "group": row.group_name,
        })

    # ========================================
    # ЭФИРНОЕ СОСТОЯНИЕ
    # ========================================

    state = state_response(session).model_dump(
        mode="json"
    )

    state.update({
        "home_score": match.home_score or 0,
        "away_score": match.away_score or 0,
        "home_team_name": home["name"],
        "away_team_name": away["name"],
        "home_team_short_name": home["short_name"],
        "away_team_short_name": away["short_name"],
        "home_team_logo_url": home["logo_url"],
        "away_team_logo_url": away["logo_url"],
    })

    tournament = match.tournament

    return {
        "state": state,

        "match": {
            "id": match.id,
            "date": (
                match.match_date.isoformat()
                if match.match_date else None
            ),
            "stadium": match.stadium,
            "round": match.round,
            "group": match.group_name,
            "stage": enum_value(match.stage),
            "official_home_score": match.home_score,
            "official_away_score": match.away_score,
            "home_penalty": match.home_penalty,
            "away_penalty": match.away_penalty,
            "tournament": (
                tournament.name
                if tournament else None
            ),
            "tournament_short_name": (
                tournament.short_name
                if tournament else None
            ),
        },

        "teams": {
            "home": home,
            "away": away,
        },

        "events": events,
        "standings": standings,
    }
