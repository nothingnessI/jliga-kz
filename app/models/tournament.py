from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Integer, String, DateTime, Enum, UniqueConstraint

from app.core import Base, TimestampMixin

from datetime import datetime
from app.models.enums import TournamentStatus, TournamentFormat

from app.models.match import Match
"""
Модель турниров
"""

class Tournament(Base, TimestampMixin):
    __tablename__ = "tournaments"

    __table_args__ = (
        UniqueConstraint("name", "season", name="uq_tournament_name_season"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(500), nullable=True, index=True)
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    division: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    season: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    age_category: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[TournamentStatus] = mapped_column(
        Enum(TournamentStatus),
        default=TournamentStatus.DRAFT,
        nullable=False,
    )
    max_teams: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=False, index=True)
    tournament_format: Mapped[TournamentFormat | None] = mapped_column(
        Enum(TournamentFormat)
    )

    # relationships

    matches: Mapped[list["Match"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan"
    )

    standings: Mapped[list["TournamentStanding"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan"
    )

    tournament_teams: Mapped[list["TournamentTeam"]] = relationship(
        back_populates="tournament",
        cascade="all, delete-orphan",
    )

    news: Mapped[list["News"]] = relationship(
        back_populates="tournament",
    )

    documents: Mapped[list["Document"]] = relationship(
        back_populates="tournament",
    )

    disqualifications: Mapped[list["Disqualification"]] = relationship(
        back_populates="tournament", cascade="all, delete-orphan"
    )

    