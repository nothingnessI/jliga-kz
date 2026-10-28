from datetime import date

from sqlalchemy import Date, Enum, String, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.enums import PlayerPosition
from app.core import Base, TimestampMixin


class Player(TimestampMixin, Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(primary_key=True)
    jliga_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True, index=True)
    first_name: Mapped[str] = mapped_column(String(100), nullable=False)
    last_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date)
    position: Mapped[PlayerPosition | None] = mapped_column(
        Enum(PlayerPosition, name="player_position")
    )
    photo_url: Mapped[str | None] = mapped_column(String(500))

    # Связи
    teams: Mapped[list["TeamPlayer"]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )
    match_events: Mapped[list["MatchEvent"]] = relationship(back_populates="player")
    disqualifications: Mapped[list["Disqualification"]] = relationship(
        back_populates="player", cascade="all, delete-orphan"
    )