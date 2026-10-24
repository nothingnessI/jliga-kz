from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base, TimestampMixin


class MediaAlbum(TimestampMixin, Base):
    __tablename__ = "media_albums"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    tournament_id: Mapped[int | None] = mapped_column(
        ForeignKey("tournaments.id", ondelete="SET NULL"), nullable=True
    )

    # Связи
    items: Mapped[list["MediaItem"]] = relationship(
        back_populates="album", cascade="all, delete-orphan"
    )


class MediaItem(TimestampMixin, Base):
    __tablename__ = "media_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    album_id: Mapped[int] = mapped_column(
        ForeignKey("media_albums.id", ondelete="CASCADE"), nullable=False, index=True
    )
    media_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "photo" или "video"
    file_url: Mapped[str] = mapped_column(String(500), nullable=False)

    # Связи
    album: Mapped["MediaAlbum"] = relationship(back_populates="items")