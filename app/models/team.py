from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import Base, TimestampMixin


class Team(TimestampMixin, Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    city: Mapped[str | None] = mapped_column(String(100))
    logo_url: Mapped[str | None] = mapped_column(String(500))
    coach_name: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Связи
    players: Mapped[list["TeamPlayer"]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )
    tournaments: Mapped[list["TournamentTeam"]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )
    home_matches: Mapped[list["Match"]] = relationship(
        back_populates="home_team",
        foreign_keys="Match.home_team_id",
        cascade="all, delete-orphan",
    )
    away_matches: Mapped[list["Match"]] = relationship(
        back_populates="away_team",
        foreign_keys="Match.away_team_id",
        cascade="all, delete-orphan",
    )
    standings: Mapped[list["TournamentStanding"]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )