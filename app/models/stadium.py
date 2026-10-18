from sqlalchemy import String, Integer, Float
from sqlalchemy.orm import Mapped, mapped_column

from app.core import TimestampMixin, Base


class Stadium(TimestampMixin, Base):
    __tablename__ = "stadiums"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(String(300))
    city: Mapped[str | None] = mapped_column(String(100))
    capacity: Mapped[int | None] = mapped_column(Integer)
    image_url: Mapped[str | None] = mapped_column(String(500))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)