
from datetime import datetime, timezone
from typing import Any, TypeVar

from fastapi import HTTPException
from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.broadcast import (
    BroadcastCommand,
    BroadcastGraphicType,
    BroadcastPhase,
    BroadcastSession,
)
from app.models.match import Match
from app.schemas.broadcast import (
    BroadcastAddedTimeSet,
    BroadcastClockSet,
    BroadcastCommandRequest,
    BroadcastGraphicShow,
    BroadcastPhaseSet,
    BroadcastScoreUpdate,
    BroadcastStateResponse,
)


MAX_CLOCK_MS = 180 * 60 * 1000

SchemaT = TypeVar("SchemaT", bound=BaseModel)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)

    return value.astimezone(timezone.utc)


def elapsed_now(
    session: BroadcastSession,
    now: datetime | None = None,
) -> int:
    elapsed = max(0, session.elapsed_ms or 0)

    if not session.clock_running:
        return min(elapsed, MAX_CLOCK_MS)

    if session.clock_started_at is None:
        return min(elapsed, MAX_CLOCK_MS)

    now = now or utc_now()

    difference = int(
        (
            now - as_utc(session.clock_started_at)
        ).total_seconds() * 1000
    )

    return min(
        MAX_CLOCK_MS,
        elapsed + max(0, difference),
    )


def parse_payload(
    schema: type[SchemaT],
    payload: dict[str, Any],
) -> SchemaT:
    try:
        return schema.model_validate(payload)

    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail=exc.errors(
                include_context=False,
                include_input=False,
            ),
        ) from exc


def state_response(
    session: BroadcastSession,
) -> BroadcastStateResponse:
    now = utc_now()

    return BroadcastStateResponse(
        id=session.id,
        match_id=session.match_id,
        is_active=session.is_active,
        phase=session.phase,
        home_score=session.home_score,
        away_score=session.away_score,
        elapsed_ms=session.elapsed_ms,
        clock_running=session.clock_running,
        clock_started_at=session.clock_started_at,
        added_time_minutes=session.added_time_minutes,
        scoreboard_visible=session.scoreboard_visible,
        active_graphic=session.active_graphic,
        graphic_payload=session.graphic_payload or {},
        revision=session.revision,
        server_time=now,
        current_elapsed_ms=elapsed_now(session, now),
    )


class BroadcastService:

    @staticmethod
    async def create_session(
        db: AsyncSession,
        match_id: int,
    ) -> BroadcastSession:
        """
        Одна сессия на матч.

        PostgreSQL ON CONFLICT защищает от двух
        одновременных запросов создания трансляции.
        """
        match = await db.get(Match, match_id)

        if match is None:
            raise HTTPException(
                status_code=404,
                detail="Матч не найден",
            )

        stmt = (
            pg_insert(BroadcastSession)
            .values(
                match_id=match_id,
                is_active=False,
                phase=BroadcastPhase.PRE_MATCH.value,
                home_score=match.home_score or 0,
                away_score=match.away_score or 0,
                elapsed_ms=0,
                clock_running=False,
                clock_started_at=None,
                added_time_minutes=0,
                scoreboard_visible=True,
                active_graphic=BroadcastGraphicType.NONE.value,
                graphic_payload={},
                revision=0,
            )
            .on_conflict_do_nothing(
                index_elements=["match_id"],
            )
        )

        await db.execute(stmt)

        session = await db.scalar(
            select(BroadcastSession).where(
                BroadcastSession.match_id == match_id
            )
        )

        if session is None:
            raise HTTPException(
                status_code=500,
                detail="Не удалось создать трансляцию",
            )

        return session

    @staticmethod
    async def get_session(
        db: AsyncSession,
        session_id: int,
        for_update: bool = False,
    ) -> BroadcastSession:
        stmt = select(BroadcastSession).where(
            BroadcastSession.id == session_id
        )

        if for_update:
            stmt = stmt.with_for_update()

        session = await db.scalar(stmt)

        if session is None:
            raise HTTPException(
                status_code=404,
                detail="Трансляция не найдена",
            )

        return session

    @staticmethod
    async def execute_command(
        db: AsyncSession,
        session_id: int,
        command: BroadcastCommandRequest,
        operator_id: int | None = None,
    ) -> BroadcastStateResponse:
        """
        Изменяет эфирное состояние.

        Внешний API отвечает за транзакцию,
        официальный Match и отправку по WebSocket.
        """
        session = await BroadcastService.get_session(
            db,
            session_id,
            for_update=True,
        )

        if session.revision != command.expected_revision:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "Состояние эфира изменилось",
                    "current_revision": session.revision,
                },
            )

        action = command.action
        payload = command.payload
        now = utc_now()

        if action == "START_CLOCK":
            if session.phase == BroadcastPhase.FINISHED.value:
                raise HTTPException(
                    status_code=409,
                    detail="Матч завершён",
                )

            if not session.clock_running:
                session.clock_started_at = now
                session.clock_running = True

        elif action == "PAUSE_CLOCK":
            if session.clock_running:
                session.elapsed_ms = elapsed_now(
                    session,
                    now,
                )
                session.clock_running = False
                session.clock_started_at = None

        elif action == "RESET_CLOCK":
            session.elapsed_ms = 0
            session.clock_running = False
            session.clock_started_at = None

        elif action == "SET_CLOCK":
            data = parse_payload(
                BroadcastClockSet,
                payload,
            )

            session.elapsed_ms = data.elapsed_ms

            if session.clock_running:
                session.clock_started_at = now

        elif action == "SET_SCORE":
            data = parse_payload(
                BroadcastScoreUpdate,
                payload,
            )

            session.home_score = data.home_score
            session.away_score = data.away_score

        elif action == "SET_PHASE":
            data = parse_payload(
                BroadcastPhaseSet,
                payload,
            )

            session.phase = data.phase.value

            if data.phase == BroadcastPhase.FINISHED:
                session.elapsed_ms = elapsed_now(
                    session,
                    now,
                )
                session.clock_running = False
                session.clock_started_at = None

        elif action == "SET_ADDED_TIME":
            data = parse_payload(
                BroadcastAddedTimeSet,
                payload,
            )

            session.added_time_minutes = data.minutes

        elif action == "SHOW_SCOREBOARD":
            session.scoreboard_visible = True

        elif action == "HIDE_SCOREBOARD":
            session.scoreboard_visible = False

        elif action == "SHOW_GRAPHIC":
            data = parse_payload(
                BroadcastGraphicShow,
                payload,
            )

            if data.graphic_type == BroadcastGraphicType.NONE:
                raise HTTPException(
                    status_code=422,
                    detail="Укажите графику",
                )

            session.active_graphic = data.graphic_type.value
            session.graphic_payload = data.payload

        elif action == "HIDE_GRAPHIC":
            session.active_graphic = BroadcastGraphicType.NONE.value
            session.graphic_payload = {}

        elif action == "ACTIVATE":
            session.is_active = True

        elif action == "DEACTIVATE":
            session.elapsed_ms = elapsed_now(
                session,
                now,
            )
            session.clock_running = False
            session.clock_started_at = None
            session.is_active = False
            session.active_graphic = BroadcastGraphicType.NONE.value
            session.graphic_payload = {}

        else:
            raise HTTPException(
                status_code=422,
                detail="Неизвестная команда",
            )

        session.revision += 1

        db.add(
            BroadcastCommand(
                session_id=session.id,
                revision=session.revision,
                command_type=action,
                payload=payload,
                operator_id=operator_id,
            )
        )

        await db.flush()

        return state_response(session)
