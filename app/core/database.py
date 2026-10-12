from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import func,DateTime


"""
Файл декларации базы данных
"""

from datetime import datetime


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    """
    Миксин для отображения даты и времени при записи и обновлении
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        onupdate=func.now(),
    )