from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.core import TimestampMixin, Base


class Stadium(TimestampMixin, Base):
    __tablename__ = "stadiums"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(100))