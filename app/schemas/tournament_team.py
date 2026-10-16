from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.base import ORMModel
from app.schemas.team import TeamReadShort


class TournamentTeamBase(BaseModel):
    tournament_id: int
    team_id: int
    group_name: str | None = Field(None, max_length=50)


class TournamentTeamCreate(TournamentTeamBase):
    pass


class TournamentTeamUpdate(BaseModel):
    group_name: str | None = Field(None, max_length=50)


class TournamentTeamRead(ORMModel):
    id: int
    tournament_id: int
    team_id: int
    group_name: str | None
    created_at: datetime
    updated_at: datetime


class TournamentTeamReadWithTeam(TournamentTeamRead):
    team: TeamReadShort