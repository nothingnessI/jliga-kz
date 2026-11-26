
"""Редактируемые рейтинги J-LIGA."""

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RatingBoard(Base):
    __tablename__ = "rating_boards"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    title: Mapped[str] = mapped_column(
        String(150),
        nullable=False,
    )

    year: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        index=True,
    )

    is_published: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class RatingColumn(Base):
    __tablename__ = "rating_columns"

    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "tournament_id",
            name="uq_rating_board_tournament",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    board_id: Mapped[int] = mapped_column(
        ForeignKey(
            "rating_boards.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    tournament_id: Mapped[int | None] = mapped_column(
        ForeignKey(
            "tournaments.id",
            ondelete="SET NULL",
        ),
        nullable=True,
    )

    label: Mapped[str] = mapped_column(
        String(200),
        nullable=False,
    )

    position: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )


class RatingRow(Base):
    __tablename__ = "rating_rows"

    __table_args__ = (
        UniqueConstraint(
            "board_id",
            "team_id",
            name="uq_rating_board_team",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
    )

    board_id: Mapped[int] = mapped_column(
        ForeignKey(
            "rating_boards.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    team_id: Mapped[int] = mapped_column(
        ForeignKey(
            "teams.id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )

    points: Mapped[dict] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
