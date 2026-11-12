from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import TournamentFormat, TournamentStatus
from app.schemas.base import ORMModel


class TournamentBase(BaseModel):
    name: str = Field(..., min_length=3, max_length=200)

    # НОВЫЕ ПОЛЯ
    short_name: str | None = Field(None, max_length=100)
    show_in_menu: bool = True

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
    sort_order: int = 0
    tie_breakers: list | None = None
    custom_groups: list | None = None
    menu_category: str | None = None


class TournamentCreate(TournamentBase):
    pass


class TournamentUpdate(BaseModel):
    name: str | None = Field(None, min_length=3, max_length=200)

    # НОВЫЕ ПОЛЯ (для обновления)
    short_name: str | None = Field(None, max_length=100)
    show_in_menu: bool | None = None

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
    sort_order: int | None = None
    tie_breakers: list | None = None
    custom_groups: list | None = None
    menu_category: str | None = None


class TournamentRead(ORMModel):
    id: int
    name: str

    # НОВЫЕ ПОЛЯ (для чтения)
    short_name: str | None
    show_in_menu: bool

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
    sort_order: int
    tie_breakers: list | None
    custom_groups: list | None
    created_at: datetime
    updated_at: datetime
    menu_category: str | None


class TournamentReadShort(ORMModel):
    id: int
    name: str

    # НОВЫЕ ПОЛЯ (для краткого чтения)
    short_name: str | None
    show_in_menu: bool

    season: str
    division: str
    age_category: str
    status: TournamentStatus
    logo_url: str | None
    menu_category: str | None