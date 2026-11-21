
import enum
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class RegistrationStatus(str, enum.Enum):
    DRAFT = "draft"
    SUBMITTED = "submitted"
    REVISION = "revision"
    APPROVED = "approved"
    REJECTED = "rejected"


class Registration(Base):
    __tablename__ = "coach_registrations"

    id: Mapped[int] = mapped_column(primary_key=True)

    author_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default=RegistrationStatus.DRAFT.value,
        nullable=False,
    )

    coach_first_name: Mapped[str | None] = mapped_column(String(100))
    coach_last_name: Mapped[str | None] = mapped_column(String(100))
    coach_birth_year: Mapped[int | None] = mapped_column(Integer)
    coach_photo_url: Mapped[str | None] = mapped_column(String(500))

    admin_comment: Mapped[str | None] = mapped_column(Text)

    reviewed_by_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )

    submitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    players: Mapped[list["RegistrationPlayer"]] = relationship(
        back_populates="registration",
        cascade="all, delete-orphan",
        order_by="RegistrationPlayer.id",
    )


class RegistrationPlayer(Base):
    __tablename__ = "coach_registration_players"

    __table_args__ = (
        UniqueConstraint(
            "registration_id",
            "jersey_number",
            name="uq_registration_jersey",
        ),
        UniqueConstraint(
            "registration_id",
            "existing_player_id",
            name="uq_registration_existing_player",
        ),
        CheckConstraint(
            "jersey_number BETWEEN 1 AND 99",
            name="ck_registration_jersey",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    registration_id: Mapped[int] = mapped_column(
        ForeignKey("coach_registrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    existing_player_id: Mapped[int | None] = mapped_column(
        ForeignKey("players.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )

    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False)

    birth_date: Mapped[date] = mapped_column(Date, nullable=False)

    position: Mapped[str] = mapped_column(String(30), nullable=False)

    jersey_number: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    photo_url: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )

    registration: Mapped["Registration"] = relationship(
        back_populates="players"
    )
