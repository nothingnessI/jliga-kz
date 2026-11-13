from sqlalchemy import String, Integer, Boolean
from sqlalchemy.orm import Mapped, mapped_column
from app.core import Base, TimestampMixin


class Staff(Base, TimestampMixin):
    __tablename__ = "staff"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    position: Mapped[str] = mapped_column(String(200), nullable=False)  # Например: Президент Лиги
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    social_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    rank: Mapped[str] = mapped_column(String(200), nullable=False, default="Руководители")

    sort_order: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)