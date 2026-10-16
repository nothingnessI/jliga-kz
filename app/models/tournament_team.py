from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core import Base, TimestampMixin


class TournamentTeam(TimestampMixin, Base):
    __tablename__ = "tournament_teams"
    __table_args__ = (
        UniqueConstraint("tournament_id", "team_id", name="uq_tournament_team"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_name: Mapped[str | None] = mapped_column(String(50))

    # Связи
    tournament: Mapped["Tournament"] = relationship(
        back_populates="tournament_teams"
    )
    team: Mapped["Team"] = relationship(back_populates="tournaments")