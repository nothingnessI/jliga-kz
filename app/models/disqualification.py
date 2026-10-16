from datetime import date

from sqlalchemy import Date, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import Base, TimestampMixin


class Disqualification(TimestampMixin, Base):
    __tablename__ = "disqualifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(
        ForeignKey("players.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    decision_date: Mapped[date | None] = mapped_column(Date)
    matches_total: Mapped[int | None] = mapped_column(Integer)
    matches_remaining: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    disqualified_until: Mapped[date | None] = mapped_column(Date)

    # Связи
    player: Mapped["Player"] = relationship(back_populates="disqualifications")
    tournament: Mapped["Tournament"] = relationship(back_populates="disqualifications")