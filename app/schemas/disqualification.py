from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas.base import ORMModel


class DisqualificationBase(BaseModel):
    player_id: int
    team_id: int
    tournament_id: int
    reason: str = Field(..., min_length=3, max_length=500)
    decision_date: date | None = None
    matches_total: int | None = Field(None, ge=0)
    matches_remaining: int = Field(0, ge=0)
    disqualified_until: date | None = None


class DisqualificationCreate(DisqualificationBase):
    pass


class DisqualificationUpdate(BaseModel):
    reason: str | None = Field(None, min_length=3, max_length=500)
    decision_date: date | None = None
    matches_total: int | None = Field(None, ge=0)
    matches_remaining: int | None = Field(None, ge=0)
    disqualified_until: date | None = None


class DisqualificationRead(ORMModel):
    id: int
    player_id: int
    team_id: int
    tournament_id: int
    reason: str
    decision_date: date | None
    matches_total: int | None
    matches_remaining: int
    disqualified_until: date | None
    created_at: datetime
    updated_at: datetime