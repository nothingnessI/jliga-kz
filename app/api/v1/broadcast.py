
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    WebSocketException,
    status,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.enums import MatchStatus
from app.models.match import Match
from app.schemas.broadcast import (
    BroadcastCommandRequest,
    BroadcastSessionCreate,
    BroadcastStateResponse,
)
from app.services.broadcast import (
    BroadcastService,
    state_response,
)
from app.services.broadcast_realtime import broadcast_realtime
from app.services.standings import StandingsService


log = logging.getLogger(__name__)


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


async def full_state(
    db: AsyncSession,
    session,
) -> BroadcastStateResponse:
    result = state_response(session)

    match = await db.scalar(
        select(Match)
        .options(
            selectinload(Match.home_team),
            selectinload(Match.away_team),
        )
        .where(Match.id == session.match_id)
    )

    if match is None:
        return result

    # Официальный счёт является основным.
    result.home_score = match.home_score or 0
    result.away_score = match.away_score or 0

    for side in ("home", "away"):
        team = getattr(match, f"{side}_team")

        if team is None:
            continue

        setattr(
            result,
            f"{side}_team_name",
            team.name,
        )

        setattr(
            result,
            f"{side}_team_short_name",
            team.name[:12],
        )

        setattr(
            result,
            f"{side}_team_logo_url",
            team.logo_url,
        )

    return result


async def notify(
    session_id: int,
    result: BroadcastStateResponse,
):
    try:
        await broadcast_realtime.broadcast_state(
            session_id,
            result,
        )

    except Exception:
        log.exception(
            "Broadcast notification failed: %s",
            session_id,
        )


def create_broadcast_router(
    get_db: Callable[..., Any],
    require_operator: Callable[..., Any],
    authorize_viewer: Callable[
        [WebSocket, int], Awaitable[bool]
    ],
) -> APIRouter:

    router = APIRouter(
        prefix="/broadcast",
        tags=["Broadcast Studio"],
    )

    @router.get("/matches")
    async def list_matches(
        db: AsyncSession = Depends(get_db),
        operator: Any = Depends(require_operator),
    ):
        matches = (
            await db.scalars(
                select(Match)
                .options(
                    selectinload(Match.home_team),
                    selectinload(Match.away_team),
                )
                .order_by(
                    Match.match_date.desc().nullslast(),
                    Match.id.desc(),
                )
                .limit(200)
            )
        ).all()

        return [
            {
                "id": match.id,
                "home": (
                    match.home_team.name
                    if match.home_team else "HOME"
                ),
                "away": (
                    match.away_team.name
                    if match.away_team else "AWAY"
                ),
                "date": (
                    match.match_date.isoformat()
                    if match.match_date else None
                ),
            }
            for match in matches
        ]

    @router.post(
        "/sessions",
        response_model=BroadcastStateResponse,
        status_code=201,
    )
    async def create_session(
        data: BroadcastSessionCreate,
        request: Request,
        db: AsyncSession = Depends(get_db),
        operator: Any = Depends(require_operator),
    ):
        check_origin(request)

        try:
            session = await BroadcastService.create_session(
                db,
                data.match_id,
            )

            result = await full_state(
                db,
                session,
            )

            await db.commit()
            return result

        except Exception:
            await db.rollback()
            raise

    @router.get(
        "/sessions/{session_id}",
        response_model=BroadcastStateResponse,
    )
    async def get_session(
        session_id: int,
        db: AsyncSession = Depends(get_db),
        operator: Any = Depends(require_operator),
    ):
        session = await BroadcastService.get_session(
            db,
            session_id,
        )

        return await full_state(
            db,
            session,
        )

    @router.get(
        "/sessions/{session_id}/public",
        response_model=BroadcastStateResponse,
    )
    async def get_public_session(
        session_id: int,
        response: Response,
        db: AsyncSession = Depends(get_db),
    ):
        response.headers["Cache-Control"] = "no-store"

        session = await BroadcastService.get_session(
            db,
            session_id,
        )

        return await full_state(
            db,
            session,
        )

    @router.post(
        "/sessions/{session_id}/commands",
        response_model=BroadcastStateResponse,
    )
    async def execute_command(
        session_id: int,
        command: BroadcastCommandRequest,
        request: Request,
        db: AsyncSession = Depends(get_db),
        operator: Any = Depends(require_operator),
    ):
        check_origin(request)

        became_finished = False
        tournament_id = None

        try:
            # 1. Команда блокирует конкретную
            # BroadcastSession через FOR UPDATE.
            await BroadcastService.execute_command(
                db=db,
                session_id=session_id,
                command=command,
                operator_id=operator.id,
            )

            session = await BroadcastService.get_session(
                db,
                session_id,
            )

            # 2. Затем блокируем официальный матч.
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

            tournament_id = match.tournament_id

            # 3. Ручное изменение счёта
            # синхронизируется с турниром.
            if command.action == "SET_SCORE":
                if match.status == MatchStatus.FINISHED:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            "Завершённый матч нельзя "
                            "изменять из Broadcast Studio"
                        ),
                    )

                match.home_score = session.home_score
                match.away_score = session.away_score

            # 4. Первый запуск переводит матч в LIVE.
            elif command.action == "START_CLOCK":
                if match.status == MatchStatus.SCHEDULED:
                    match.status = MatchStatus.LIVE

            # 5. Смена игрового периода.
            elif command.action == "SET_PHASE":
                if (
                    session.phase
                    not in ("PRE_MATCH", "FINISHED")
                    and match.status == MatchStatus.SCHEDULED
                ):
                    match.status = MatchStatus.LIVE

                if session.phase == "FINISHED":
                    became_finished = (
                        match.status != MatchStatus.FINISHED
                    )

                    match.status = MatchStatus.FINISHED

            # 6. Синхронизируем зеркало счёта.
            session.home_score = match.home_score or 0
            session.away_score = match.away_score or 0

            await db.flush()

            result = await full_state(
                db,
                session,
            )

            # 7. Всё фиксируется вместе.
            await db.commit()

        except Exception:
            await db.rollback()
            raise

        # 8. Пересчёт таблицы после завершения.
        if became_finished and tournament_id is not None:
            try:
                # Блокировка действует до COMMIT
                # внутри StandingsService.
                await db.execute(
                    text(
                        "SELECT pg_advisory_xact_lock(:tid)"
                    ),
                    {"tid": tournament_id},
                )

                await StandingsService.recalculate_standings(
                    db,
                    tournament_id,
                )

            except Exception:
                log.exception(
                    "Standings update failed: tournament=%s",
                    tournament_id,
                )

                await db.rollback()

        # 9. WebSocket вызывается после фиксации БД.
        await notify(session_id, result)

        return result

    @router.websocket(
        "/sessions/{session_id}/ws"
    )
    async def websocket_session(
        websocket: WebSocket,
        session_id: int,
        db: AsyncSession = Depends(get_db),
    ):
        allowed = await authorize_viewer(
            websocket,
            session_id,
        )

        if not allowed:
            raise WebSocketException(
                code=status.WS_1008_POLICY_VIOLATION,
            )

        session = await BroadcastService.get_session(
            db,
            session_id,
        )

        initial = await full_state(
            db,
            session,
        )

        await broadcast_realtime.connect(
            session_id,
            websocket,
        )

        try:
            await websocket.send_json({
                "type": "broadcast.state",
                "data": initial.model_dump(
                    mode="json"
                ),
            })

            while True:
                await websocket.receive_text()

        except WebSocketDisconnect:
            pass

        finally:
            await broadcast_realtime.disconnect(
                session_id,
                websocket,
            )

    return router
