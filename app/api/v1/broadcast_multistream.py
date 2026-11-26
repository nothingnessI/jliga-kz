
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin
from app.db.session import get_db
from app.models.broadcast import (
    BroadcastCommand,
    BroadcastSession,
)
from app.models.enums import MatchStatus
from app.models.match import Match
from app.models.tournament import Tournament
from app.services.broadcast import (
    BroadcastService,
    state_response,
)
from app.services.broadcast_realtime import broadcast_realtime


router = APIRouter(
    prefix="/broadcast",
    tags=["Broadcast Multistream"],
)


class StartRequest(BaseModel):
    match_id: int = Field(gt=0)


class SyncRequest(BaseModel):
    expected_revision: int = Field(ge=0)


def check_origin(request: Request):
    auth = request.headers.get("authorization", "")

    if auth.lower().startswith("bearer "):
        return

    source = (
        request.headers.get("origin")
        or request.headers.get("referer")
        or ""
    )

    parsed = urlsplit(source)

    if (
        parsed.scheme not in ("http", "https")
        or parsed.netloc.lower()
        != request.headers.get("host", "").lower()
    ):
        raise HTTPException(
            status_code=403,
            detail="Недопустимый источник запроса",
        )


def session_json(session, match):
    return {
        "id": session.id,
        "match_id": match.id,
        "tournament_id": match.tournament_id,
        "tournament": (
            match.tournament.name
            if match.tournament else "Турнир"
        ),
        "home": (
            match.home_team.name
            if match.home_team else "HOME"
        ),
        "away": (
            match.away_team.name
            if match.away_team else "AWAY"
        ),
        "home_score": match.home_score,
        "away_score": match.away_score,
        "status": match.status.value,
        "phase": session.phase,
        "is_active": session.is_active,
        "revision": session.revision,
        "match_date": (
            match.match_date.isoformat()
            if match.match_date else None
        ),
    }


@router.get("/control/sessions")
async def list_sessions(
    tournament_id: int | None = None,
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),
):
    stmt = (
        select(BroadcastSession, Match)
        .join(
            Match,
            Match.id == BroadcastSession.match_id,
        )
        .options(
            selectinload(Match.home_team),
            selectinload(Match.away_team),
            selectinload(Match.tournament),
        )
        .order_by(
            BroadcastSession.is_active.desc(),
            Match.match_date.desc().nullslast(),
            BroadcastSession.id.desc(),
        )
        .limit(max(1, min(limit, 500)))
    )

    if tournament_id is not None:
        stmt = stmt.where(
            Match.tournament_id == tournament_id
        )

    rows = (await db.execute(stmt)).all()

    return [
        session_json(session, match)
        for session, match in rows
    ]

from datetime import time, timedelta, datetime, timezone
from zoneinfo import ZoneInfo
from sqlalchemy import select, case



@router.get("/control/matches")
async def list_matches(
    limit: int = 200,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),
):
    local_tz = ZoneInfo("Asia/Almaty")

    now_local = datetime.now(local_tz)
    now_utc = now_local.astimezone(timezone.utc)

    # Три дня назад + сегодня + три дня вперёд.
    start_local = datetime.combine(
        now_local.date() - timedelta(days=3),
        time.min,
        tzinfo=local_tz,
    )

    end_local = datetime.combine(
        now_local.date() + timedelta(days=4),
        time.min,
        tzinfo=local_tz,
    )

    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    # Будущие матчи отображаются первыми.
    future_first = case(
        (Match.match_date >= now_utc, 0),
        else_=1,
    )

    stmt = (
        select(Match)
        .options(
            selectinload(Match.home_team),
            selectinload(Match.away_team),
            selectinload(Match.tournament),
        )
        .where(
            Match.match_date >= start_utc,
            Match.match_date < end_utc,
        )
        .order_by(
            future_first.asc(),
            case(
                (
                    Match.match_date >= now_utc,
                    Match.match_date,
                ),
                else_=None,
            ).asc().nullslast(),
            case(
                (
                    Match.match_date < now_utc,
                    Match.match_date,
                ),
                else_=None,
            ).desc().nullslast(),
            Match.id.asc(),
        )
        .limit(max(1, min(limit, 500)))
    )

    matches = (await db.scalars(stmt)).all()

    return [
        {
            "id": match.id,
            "tournament_id": match.tournament_id,
            "tournament": (
                match.tournament.name
                if match.tournament
                else "Турнир"
            ),
            "home": (
                match.home_team.name
                if match.home_team
                else "HOME"
            ),
            "away": (
                match.away_team.name
                if match.away_team
                else "AWAY"
            ),
            "date": (
                match.match_date.isoformat()
                if match.match_date
                else None
            ),
            "status": match.status.value,
        }
        for match in matches
    ]



@router.post("/control/start")
async def start_broadcast(
    data: StartRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),
):
    check_origin(request)

    try:
        session = await BroadcastService.create_session(
            db,
            data.match_id,
        )

        result = {
            "id": session.id,
            "match_id": session.match_id,
        }

        await db.commit()

        return result

    except Exception:
        await db.rollback()
        raise


@router.post(
    "/control/sessions/{session_id}/sync"
)
async def sync_score(
    session_id: int,
    data: SyncRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),
):
    check_origin(request)

    try:
        session = await BroadcastService.get_session(
            db,
            session_id,
            for_update=True,
        )

        if session.revision != data.expected_revision:
            raise HTTPException(
                status_code=409,
                detail="Состояние эфира изменилось",
            )

        match = await db.scalar(
            select(Match)
            .where(Match.id == session.match_id)
            .with_for_update()
        )

        if match is None:
            raise HTTPException(
                status_code=404,
                detail="Матч не найден",
            )

        session.home_score = match.home_score or 0
        session.away_score = match.away_score or 0

        session.revision += 1

        db.add(
            BroadcastCommand(
                session_id=session.id,
                revision=session.revision,
                command_type="SYNC_OFFICIAL_SCORE",
                payload={
                    "home_score": session.home_score,
                    "away_score": session.away_score,
                },
                operator_id=admin.id,
            )
        )

        await db.flush()

        result = state_response(session)

        await db.commit()

    except Exception:
        await db.rollback()
        raise

    await broadcast_realtime.broadcast_state(
        session_id,
        result,
    )

    return result


@router.get(
    "/tournaments/{tournament_id}/live"
)
async def tournament_live(
    tournament_id: int,
    response: Response,
    db: AsyncSession = Depends(get_db),
):
    response.headers["Cache-Control"] = "no-store"

    tournament = await db.get(
        Tournament,
        tournament_id,
    )

    if tournament is None:
        raise HTTPException(
            status_code=404,
            detail="Турнир не найден",
        )

    stmt = (
        select(Match, BroadcastSession)
        .outerjoin(
            BroadcastSession,
            BroadcastSession.match_id == Match.id,
        )
        .where(
            Match.tournament_id == tournament_id,
            Match.status.notin_([
                MatchStatus.FINISHED,
                MatchStatus.CANCELLED,
            ]),
            or_(
                Match.status == MatchStatus.LIVE,
                BroadcastSession.is_active.is_(True),
            ),
        )
        .options(
            selectinload(Match.home_team),
            selectinload(Match.away_team),
        )
        .order_by(
            Match.match_date.asc().nullslast(),
            Match.id.asc(),
        )
        .limit(100)
    )

    rows = (await db.execute(stmt)).all()

    matches = []

    for match, session in rows:
        matches.append({
            "id": match.id,
            "broadcast_id": (
                session.id if session else None
            ),
            "home": (
                match.home_team.name
                if match.home_team else "HOME"
            ),
            "away": (
                match.away_team.name
                if match.away_team else "AWAY"
            ),
            "home_score": match.home_score,
            "away_score": match.away_score,
            "status": match.status.value,
            "phase": (
                session.phase if session else None
            ),
            "revision": (
                session.revision if session else None
            ),
        })

    return {
        "tournament_id": tournament.id,
        "tournament": tournament.name,
        "matches": matches,
    }
