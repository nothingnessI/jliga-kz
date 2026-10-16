from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import TournamentFormat, TournamentStatus
from app.schemas.base import ORMModel


class TournamentBase(BaseModel):
    name: str = Field(..., min_length=3, max_length=200)
    description: str | None = Field(None, max_length=2000)
    start_at: datetime | None = None
    end_at: datetime | None = None
    division: str = Field(..., min_length=2, max_length=50)
    season: str = Field(..., min_length=2, max_length=50)
    age_category: str = Field(..., min_length=2, max_length=50)
    status: TournamentStatus = TournamentStatus.DRAFT
    max_teams: int = Field(..., ge=2, le=64)
    logo_url: str | None = Field(None, max_length=500)
    tournament_format: TournamentFormat | None = None


class TournamentCreate(TournamentBase):
    pass


class TournamentUpdate(BaseModel):
    name: str | None = Field(None, min_length=3, max_length=200)
    description: str | None = Field(None, max_length=2000)
    start_at: datetime | None = None
    end_at: datetime | None = None
    division: str | None = Field(None, min_length=2, max_length=50)
    season: str | None = Field(None, min_length=2, max_length=50)
    age_category: str | None = Field(None, min_length=2, max_length=50)
    status: TournamentStatus | None = None
    max_teams: int | None = Field(None, ge=2, le=64)
    logo_url: str | None = Field(None, max_length=500)
    tournament_format: TournamentFormat | None = None


class TournamentRead(ORMModel):
    id: int
    name: str
    description: str | None
    start_at: datetime | None
    end_at: datetime | None
    division: str
    season: str
    age_category: str
    status: TournamentStatus
    max_teams: int
    logo_url: str | None
    tournament_format: TournamentFormat | None
    created_at: datetime
    updated_at: datetime


class TournamentReadShort(ORMModel):
    id: int
    name: str
    season: str
    division: str
    age_category: str
    status: TournamentStatus
    logo_url: str | None