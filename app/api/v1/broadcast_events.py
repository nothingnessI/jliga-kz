
import logging
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_admin
from app.db.session import get_db
from app.models.broadcast import (
    BroadcastCommand,
    BroadcastGraphicType,
)
from app.models.enums import MatchEventType, MatchStatus
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.team_player import TeamPlayer
from app.services.broadcast import (
    BroadcastService,
    state_response,
)
from app.services.broadcast_realtime import broadcast_realtime


log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/broadcast",
    tags=["Broadcast Events"],
)

SCORING = {
    MatchEventType.GOAL,
    MatchEventType.PENALTY_GOAL,
    MatchEventType.OWN_GOAL,
}

ALLOWED = SCORING | {
    MatchEventType.YELLOW_CARD,
    MatchEventType.RED_CARD,
}


class EventCreate(BaseModel):
    team_id: int = Field(gt=0)
    player_id: int = Field(gt=0)
    event_type: MatchEventType
    minute: int = Field(ge=0, le=120)
    expected_revision: int = Field(ge=0)
    comment: str | None = Field(
        default=None,
        max_length=300,
    )


class EventDelete(BaseModel):
    expected_revision: int = Field(ge=0)


def check_origin(request: Request):
    authorization = request.headers.get(
        "authorization",
        "",
    )

    if authorization.lower().startswith("bearer "):
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


async def get_match(
    db: AsyncSession,
    match_id: int,
    lock: bool = False,
) -> Match:
    stmt = (
        select(Match)
        .where(Match.id == match_id)
        .options(
            selectinload(Match.home_team),
            selectinload(Match.away_team),
        )
    )

    if lock:
        stmt = stmt.with_for_update()

    match = await db.scalar(stmt)

    if match is None:
        raise HTTPException(
            status_code=404,
            detail="Матч не найден",
        )

    return match


def player_name(player) -> str:
    if player is None:
        return "Неизвестный игрок"

    return (
        f"{player.last_name} {player.first_name}"
    ).strip()


async def get_events(
    db: AsyncSession,
    match_id: int,
) -> list[dict]:
    rows = (
        await db.scalars(
            select(MatchEvent)
            .where(MatchEvent.match_id == match_id)
            .options(
                selectinload(MatchEvent.player),
                selectinload(MatchEvent.team),
            )
            .order_by(
                MatchEvent.minute.desc(),
                MatchEvent.id.desc(),
            )
        )
    ).all()

    return [
        {
            "id": event.id,
            "match_id": event.match_id,
            "team_id": event.team_id,
            "team_name": (
                event.team.name if event.team else ""
            ),
            "player_id": event.player_id,
            "player_name": player_name(event.player),
            "event_type": event.event_type.value,
            "minute": event.minute,
            "comment": event.comment,
        }
        for event in rows
    ]


def apply_goal(
    match: Match,
    session,
    team_id: int,
    event_type: MatchEventType,
    delta: int,
):
    """
    +1 — регистрация гола.
    -1 — удаление ранее зарегистрированного гола.

    Автогол увеличивает счёт соперника.
    """
    if event_type not in SCORING:
        return

    if team_id not in (
        match.home_team_id,
        match.away_team_id,
    ):
        raise HTTPException(
            status_code=422,
            detail="Команда не участвует в матче",
        )

    scored_team = team_id

    if event_type == MatchEventType.OWN_GOAL:
        scored_team = (
            match.away_team_id
            if team_id == match.home_team_id
            else match.home_team_id
        )

    if scored_team == match.home_team_id:
        new_score = (match.home_score or 0) + delta

        if new_score < 0:
            raise HTTPException(
                status_code=409,
                detail="Отрицательный счёт невозможен",
            )

        match.home_score = new_score

    else:
        new_score = (match.away_score or 0) + delta

        if new_score < 0:
            raise HTTPException(
                status_code=409,
                detail="Отрицательный счёт невозможен",
            )

        match.away_score = new_score

    session.home_score = match.home_score or 0
    session.away_score = match.away_score or 0


def complete_state(session, match):
    result = state_response(session)

    for side in ("home", "away"):
        team = getattr(match, f"{side}_team")

        if team is None:
            continue

        setattr(result, f"{side}_team_name", team.name)
        setattr(result, f"{side}_team_short_name", team.name[:12])
        setattr(result, f"{side}_team_logo_url", team.logo_url)

    return result


async def notify(session_id, state):
    try:
        await broadcast_realtime.broadcast_state(
            session_id,
            state,
        )

    except Exception:
        log.exception(
            "Broadcast notification failed: %s",
            session_id,
        )


@router.get("/sessions/{session_id}/roster")
async def roster(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),
):
    session = await BroadcastService.get_session(
        db,
        session_id,
    )

    match = await get_match(
        db,
        session.match_id,
    )

    registrations = (
        await db.scalars(
            select(TeamPlayer)
            .where(
                TeamPlayer.tournament_id == match.tournament_id,
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

    players = {
        match.home_team_id: [],
        match.away_team_id: [],
    }

    for registration in registrations:
        player = registration.player

        if player is None:
            continue

        players[registration.team_id].append({
            "id": registration.player_id,
            "team_id": registration.team_id,
            "name": player_name(player),
            "number": registration.jersey_number,
            "position": (
                player.position.value
                if player.position else None
            ),
            "captain": registration.is_captain,
        })

    return {
        "match_id": match.id,
        "tournament_id": match.tournament_id,
        "home": {
            "id": match.home_team_id,
            "name": match.home_team.name,
            "players": players[match.home_team_id],
        },
        "away": {
            "id": match.away_team_id,
            "name": match.away_team.name,
            "players": players[match.away_team_id],
        },
    }


@router.get("/sessions/{session_id}/events")
async def list_events(
    session_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),
):
    session = await BroadcastService.get_session(
        db,
        session_id,
    )

    return await get_events(
        db,
        session.match_id,
    )


@router.post(
    "/sessions/{session_id}/events",
    status_code=201,
)
async def create_event(
    session_id: int,
    data: EventCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin=Depends(get_current_admin),
):
    check_origin(request)

    if data.event_type not in ALLOWED:
        raise HTTPException(
            status_code=422,
            detail="Недопустимый тип события",
        )

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

        match = await get_match(
            db,
            session.match_id,
            lock=True,
        )

        if match.status == MatchStatus.FINISHED:
            raise HTTPException(
                status_code=409,
                detail="Матч уже завершён",
            )

        if data.team_id not in (
            match.home_team_id,
            match.away_team_id,
        ):
            raise HTTPException(
                status_code=422,
                detail="Команда не участвует в матче",
            )

        membership = await db.scalar(
            select(TeamPlayer.id).where(
                TeamPlayer.tournament_id == match.tournament_id,
                TeamPlayer.team_id == data.team_id,
                TeamPlayer.player_id == data.player_id,
                TeamPlayer.is_active.is_(True),
            )
        )

        if membership is None:
            raise HTTPException(
                status_code=422,
                detail="Игрок не заявлен за эту команду",
            )

        duplicate = await db.scalar(
            select(MatchEvent.id).where(
                MatchEvent.match_id == match.id,
                MatchEvent.player_id == data.player_id,
                MatchEvent.event_type == data.event_type,
                MatchEvent.minute == data.minute,
            )
        )

        if duplicate is not None:
            raise HTTPException(
                status_code=409,
                detail="Такое событие уже записано",
            )

        event = MatchEvent(
            match_id=match.id,
            team_id=data.team_id,
            player_id=data.player_id,
            event_type=data.event_type,
            minute=data.minute,
            comment=data.comment,
        )

        db.add(event)
        await db.flush()

        if match.status == MatchStatus.SCHEDULED:
            match.status = MatchStatus.LIVE

        apply_goal(
            match,
            session,
            data.team_id,
            data.event_type,
            +1,
        )

        registration = await db.scalar(
            select(TeamPlayer)
            .where(
                TeamPlayer.tournament_id == match.tournament_id,
                TeamPlayer.team_id == data.team_id,
                TeamPlayer.player_id == data.player_id,
            )
            .options(selectinload(TeamPlayer.player))
        )

        name = player_name(
            registration.player if registration else None
        )

        team_name = (
            match.home_team.name
            if data.team_id == match.home_team_id
            else match.away_team.name
        )

        types = {
            MatchEventType.GOAL: BroadcastGraphicType.GOAL,
            MatchEventType.OWN_GOAL: BroadcastGraphicType.GOAL,
            MatchEventType.PENALTY_GOAL: BroadcastGraphicType.GOAL,
            MatchEventType.YELLOW_CARD: BroadcastGraphicType.YELLOW_CARD,
            MatchEventType.RED_CARD: BroadcastGraphicType.RED_CARD,
        }

        captions = {
            MatchEventType.GOAL: "Гол",
            MatchEventType.OWN_GOAL: "Автогол",
            MatchEventType.PENALTY_GOAL: "Гол с пенальти",
            MatchEventType.YELLOW_CARD: "Жёлтая карточка",
            MatchEventType.RED_CARD: "Красная карточка",
        }

        session.active_graphic = types[data.event_type].value

        session.graphic_payload = {
            "event_id": event.id,
            "player_name": name,
            "team_name": team_name,
            "minute": data.minute,
            "text": captions[data.event_type],
        }

        session.revision += 1

        db.add(
            BroadcastCommand(
                session_id=session.id,
                revision=session.revision,
                command_type="MATCH_EVENT_ADD",
                payload={
                    "event_id": event.id,
                    "event_type": data.event_type.value,
                    "player_id": data.player_id,
                },
                operator_id=admin.id,
            )
        )

        await db.flush()

        result = complete_state(session, match)
        events = await get_events(db, match.id)
        created_id = event.id

        await db.commit()

    except IntegrityError as exc:
        await db.rollback()

        raise HTTPException(
            status_code=409,
            detail="Дублирующееся событие",
        ) from exc

    except Exception:
        await db.rollback()
        raise

    await notify(session_id, result)

    return {
        "event_id": created_id,
        "state": result,
        "events": events,
    }


@router.delete(
    "/sessions/{session_id}/events/{event_id}"
)
async def remove_event(
    session_id: int,
    event_id: int,
    data: EventDelete,
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

        match = await get_match(
            db,
            session.match_id,
            lock=True,
        )

        if match.status == MatchStatus.FINISHED:
            raise HTTPException(
                status_code=409,
                detail="Используйте админку завершённых матчей",
            )

        event = await db.scalar(
            select(MatchEvent).where(
                MatchEvent.id == event_id,
                MatchEvent.match_id == match.id,
            )
        )

        if event is None:
            raise HTTPException(
                status_code=404,
                detail="Событие не найдено",
            )

        kind = event.event_type

        saved = {
            "event_id": event.id,
            "event_type": kind.value,
            "player_id": event.player_id,
        }

        apply_goal(
            match,
            session,
            event.team_id,
            kind,
            -1,
        )

        await db.delete(event)
        await db.flush()

        if (
            (session.graphic_payload or {}).get("event_id")
            == event_id
        ):
            session.active_graphic = BroadcastGraphicType.NONE.value
            session.graphic_payload = {}

        session.revision += 1

        db.add(
            BroadcastCommand(
                session_id=session.id,
                revision=session.revision,
                command_type="MATCH_EVENT_DELETE",
                payload=saved,
                operator_id=admin.id,
            )
        )

        await db.flush()

        result = complete_state(session, match)
        events = await get_events(db, match.id)

        await db.commit()

    except Exception:
        await db.rollback()
        raise

    await notify(session_id, result)

    return {
        "state": result,
        "events": events,
    }
