
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Integer, ForeignKey, Enum, String
from app.core import Base, TimestampMixin
from app.models.enums import MatchEventType


class MatchEvent(TimestampMixin, Base):
    __tablename__ = "match_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    match_id: Mapped[int] = mapped_column(
        ForeignKey("matches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    event_type: Mapped[MatchEventType] = mapped_column(
        Enum(MatchEventType, name="match_event_type"),
        nullable=False,
        index=True,
    )

    minute: Mapped[int] = mapped_column(Integer, nullable=False)
    comment: Mapped[str | None] = mapped_column(String(300), nullable=True)

    # Связи
    match: Mapped["Match"] = relationship(back_populates="events")
    player: Mapped["Player"] = relationship(back_populates="match_events")

    team: Mapped["Team"] = relationship("Team")