from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.base import ORMModel


class NewsBase(BaseModel):
    title: str = Field(..., min_length=3, max_length=300)
    slug: str = Field(..., min_length=3, max_length=300, pattern=r"^[a-z0-9-]+$")
    content: str = Field(..., min_length=10)
    image_url: str | None = Field(None, max_length=500)
    published_at: datetime | None = None
    is_published: bool = False
    tournament_id: int | None = None


class NewsCreate(NewsBase):
    pass


class NewsUpdate(BaseModel):
    title: str | None = Field(None, min_length=3, max_length=300)
    slug: str | None = Field(None, min_length=3, max_length=300, pattern=r"^[a-z0-9-]+$")
    content: str | None = Field(None, min_length=10)
    image_url: str | None = Field(None, max_length=500)
    published_at: datetime | None = None
    is_published: bool | None = None
    tournament_id: int | None = None


class NewsRead(ORMModel):
    id: int
    title: str
    slug: str
    content: str
    image_url: str | None
    published_at: datetime | None
    is_published: bool
    tournament_id: int | None
    created_at: datetime
    updated_at: datetime


class NewsReadShort(ORMModel):
    id: int
    title: str
    slug: str
    image_url: str | None
    published_at: datetime | None