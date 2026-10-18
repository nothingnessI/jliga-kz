from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import Integer, String, Text, DateTime, Boolean, ForeignKey

from app.core import Base, TimestampMixin

from datetime import datetime

from app.models.tournament import Tournament


class News(Base, TimestampMixin):
    __tablename__ = "news"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    slug: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    image_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_published: Mapped[bool] = mapped_column(Boolean,default=False, index=True)
    tournament_id: Mapped[int | None] = mapped_column(
        ForeignKey("tournaments.id", ondelete="SET NULL"),
    )

    tournament: Mapped["Tournament"] = relationship(back_populates="news")