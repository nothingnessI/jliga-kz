from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import MatchStage, MatchStatus
from app.schemas.base import ORMModel
from app.schemas.team import TeamReadShort


class MatchBase(BaseModel):
    tournament_id: int
    home_team_id: int
    away_team_id: int
    match_date: datetime | None = None
    stadium: str | None = Field(None, max_length=200)
    home_score: int | None = Field(None, ge=0)
    away_score: int | None = Field(None, ge=0)
    status: MatchStatus = MatchStatus.SCHEDULED
    round: int | None = Field(None, ge=1)
    group_name: str | None = Field(None, max_length=50)
    stage: MatchStage | None = None


class MatchCreate(MatchBase):
    pass


class MatchUpdate(BaseModel):
    tournament_id: int | None = None
    home_team_id: int | None = None
    away_team_id: int | None = None
    match_date: datetime | None = None
    stadium: str | None = Field(None, max_length=200)
    home_score: int | None = Field(None, ge=0)
    away_score: int | None = Field(None, ge=0)
    status: MatchStatus | None = None
    round: int | None = Field(None, ge=1)
    group_name: str | None = Field(None, max_length=50)
    stage: MatchStage | None = None


class MatchRead(ORMModel):
    id: int
    tournament_id: int
    home_team_id: int
    away_team_id: int
    match_date: datetime | None
    stadium: str | None
    home_score: int | None
    away_score: int | None
    status: MatchStatus
    round: int | None
    group_name: str | None
    stage: MatchStage | None
    created_at: datetime
    updated_at: datetime


class MatchReadWithTeams(MatchRead):
    home_team: TeamReadShort
    away_team: TeamReadShort


class MatchReadShort(ORMModel):
    id: int
    match_date: datetime | None
    home_score: int | None
    away_score: int | None
    status: MatchStatus
    stage: MatchStage | None