from datetime import date, datetime

from pydantic import BaseModel, Field

from app.models.enums import PlayerPosition
from app.schemas.base import ORMModel


class PlayerBase(BaseModel):
    first_name: str = Field(..., min_length=1, max_length=100)
    last_name: str = Field(..., min_length=1, max_length=100)
    date_of_birth: date | None = None
    position: PlayerPosition | None = None
    photo_url: str | None = Field(None, max_length=500)


class PlayerCreate(PlayerBase):
    pass


class PlayerUpdate(BaseModel):
    first_name: str | None = Field(None, min_length=1, max_length=100)
    last_name: str | None = Field(None, min_length=1, max_length=100)
    date_of_birth: date | None = None
    position: PlayerPosition | None = None
    photo_url: str | None = Field(None, max_length=500)


class PlayerRead(ORMModel):
    id: int
    first_name: str
    last_name: str
    date_of_birth: date | None
    position: PlayerPosition | None
    photo_url: str | None
    created_at: datetime
    updated_at: datetime


class PlayerReadShort(ORMModel):
    id: int
    first_name: str
    last_name: str
    position: PlayerPosition | None
    photo_url: str | None