from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.base import ORMModel


class TeamBase(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    slug: str = Field(..., min_length=2, max_length=100, pattern=r"^[a-z0-9-]+$")
    city: str | None = Field(None, max_length=100)
    logo_url: str | None = Field(None, max_length=500)
    coach_name: str | None = Field(None, max_length=200)
    is_active: bool = True


class TeamCreate(TeamBase):
    pass


class TeamUpdate(BaseModel):
    name: str | None = Field(None, min_length=2, max_length=100)
    slug: str | None = Field(None, min_length=2, max_length=100, pattern=r"^[a-z0-9-]+$")
    city: str | None = None
    logo_url: str | None = None
    coach_name: str | None = None
    is_active: bool | None = None


class TeamRead(ORMModel):
    id: int
    name: str
    slug: str
    city: str | None
    logo_url: str | None
    coach_name: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime


class TeamReadShort(ORMModel):
    id: int
    name: str
    logo_url: str | None