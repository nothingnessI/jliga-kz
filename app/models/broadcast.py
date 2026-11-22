
"""
Модели системы управления футбольными трансляциями.

BroadcastSession — состояние эфира.
BroadcastCommand — журнал действий оператора.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class BroadcastPhase(str, Enum):
    PRE_MATCH = "PRE_MATCH"
    FIRST_HALF = "FIRST_HALF"
    HALF_TIME = "HALF_TIME"
    SECOND_HALF = "SECOND_HALF"
    EXTRA_TIME_FIRST = "EXTRA_TIME_FIRST"
    EXTRA_TIME_BREAK = "EXTRA_TIME_BREAK"
    EXTRA_TIME_SECOND = "EXTRA_TIME_SECOND"
    PENALTIES = "PENALTIES"
    FINISHED = "FINISHED"


class BroadcastGraphicType(str, Enum):
    NONE = "NONE"
    GOAL = "GOAL"
    YELLOW_CARD = "YELLOW_CARD"
    RED_CARD = "RED_CARD"
    SUBSTITUTION = "SUBSTITUTION"
    LINEUP = "LINEUP"
    BENCH = "BENCH"
    COACH = "COACH"
    STANDINGS = "STANDINGS"
    COMPARISON = "COMPARISON"
    PENALTIES = "PENALTIES"
    VAR = "VAR"
    LOWER_THIRD = "LOWER_THIRD"
    MATCH_INFO = "MATCH_INFO"


class BroadcastSession(Base):
    __tablename__ = "broadcast_sessions"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        index=True,
    )

    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        unique=True,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    phase: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=BroadcastPhase.PRE_MATCH.value,
    )

    # Счёт, который показывается в эфире.
    # Не заменяет официальный счёт Match.
    home_score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    away_score: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    # Время матча хранится в миллисекундах.
    # Пока часы остановлены, elapsed_ms
    # содержит зафиксированное время.
    elapsed_ms: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    clock_running: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    # UTC-момент запуска часов.
    clock_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    added_time_minutes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    scoreboard_visible: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
    )

    # Активная временная графика.
    active_graphic: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=BroadcastGraphicType.NONE.value,
    )

    # Например:
    # {
    #     "team_id": 10,
    #     "player_id": 25,
    #     "minute": 72
    # }
    graphic_payload: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    # Монотонная версия состояния.
    # Используется для синхронизации
    # нескольких подключённых клиентов.
    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
        onupdate=utc_now,
    )

    commands: Mapped[list["BroadcastCommand"]] = relationship(
        "BroadcastCommand",
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class BroadcastCommand(Base):
    __tablename__ = "broadcast_commands"

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "revision",
            name="uq_broadcast_command_revision",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    session_id: Mapped[int] = mapped_column(
        ForeignKey(
            "broadcast_sessions.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    revision: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    command_type: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    operator_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utc_now,
    )

    session: Mapped["BroadcastSession"] = relationship(
        "BroadcastSession",
        back_populates="commands",
    )

