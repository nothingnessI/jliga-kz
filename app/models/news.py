from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Integer, String, Text, DateTime, Boolean, ForeignKey, Table, Column
from app.core import Base, TimestampMixin
from datetime import datetime

# Таблица связи новостей и команд
news_teams = Table(
    'news_teams',
    Base.metadata,
    Column('news_id', ForeignKey('news.id', ondelete='CASCADE'), primary_key=True),
    Column('team_id', ForeignKey('teams.id', ondelete='CASCADE'), primary_key=True)
)


class News(Base, TimestampMixin):
    __tablename__ = "news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False, index=True)  # Увеличил до 300
    slug: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)  # Здесь будет HTML с таблицами
    image_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    tags: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)

    # Старая привязка к турниру (оставляем)
    tournament_id: Mapped[int | None] = mapped_column(ForeignKey("tournaments.id", ondelete="SET NULL"))
    tournament: Mapped["Tournament"] = relationship(back_populates="news")

    # Новая привязка к командам (Многие-ко-многим)
    teams: Mapped[list["Team"]] = relationship(secondary=news_teams, backref="news_articles")