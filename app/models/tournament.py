from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Integer, String, DateTime, Enum, UniqueConstraint, Boolean, JSON
from datetime import datetime

from app.core import Base, TimestampMixin
from app.models.enums import TournamentStatus, TournamentFormat

# Импорты для типизации связей (forward references)
from app.models.match import Match
from app.models.tournament_standing import TournamentStanding
from app.models.tournament_team import TournamentTeam
from app.models.news import News
from app.models.document import Document
from app.models.disqualification import Disqualification


class Tournament(Base, TimestampMixin):
    __tablename__ = "tournaments"

    __table_args__ = (
        UniqueConstraint("name", "season", name="uq_tournament_name_season"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
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
    logo_url: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    tournament_format: Mapped[TournamentFormat | None] = mapped_column(
        Enum(TournamentFormat)
    )

    jliga_id: Mapped[int | None] = mapped_column(Integer, unique=True, nullable=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    # =========================================================================
    # РЕГЛАМЕНТ ТУРНИРА
    # =========================================================================

    # 1. Начисление очков и формат
    allow_draws: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    points_win: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    points_draw: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    points_loss: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    # 2. Время матча
    periods_count: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    period_duration: Mapped[int] = mapped_column(Integer, default=45, server_default="45")

    # 3. Дисквалификации
    auto_disqualifications: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    yellow_cards_limit: Mapped[int] = mapped_column(Integer, default=4, server_default="4")
    yellow_suspension: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    red_suspension: Mapped[int] = mapped_column(Integer, default=1, server_default="1")

    # 4. Дополнительно
    tech_loss_goals: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    max_starting_players: Mapped[int] = mapped_column(Integer, default=11, server_default="11")
    max_bench_players: Mapped[int] = mapped_column(Integer, default=5, server_default="5")

    # 5. Ранжирование (список правил в порядке приоритета)
    # Пример: ["h2h_points", "goal_diff", "goals_for"]
    tie_breakers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    custom_groups: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    short_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    show_in_menu: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    menu_category: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # =========================================================================
    # СВЯЗИ (RELATIONSHIPS)
    # =========================================================================

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
        back_populates="tournament",
        cascade="all, delete-orphan"
    )