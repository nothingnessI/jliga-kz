from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.base import ORMModel
from app.schemas.team import TeamReadShort


class TournamentStandingBase(BaseModel):
    tournament_id: int
    team_id: int
    group_name: str | None = Field(None, max_length=50)


class TournamentStandingCreate(TournamentStandingBase):
    pass


class TournamentStandingRead(ORMModel):
    id: int
    tournament_id: int
    team_id: int
    group_name: str | None
    games_played: int
    wins: int
    draws: int
    losses: int
    goals_for: int
    goals_against: int
    yellow_cards: int
    red_cards: int
    clean_sheets: int
    points: int
    created_at: datetime
    updated_at: datetime


class TournamentStandingReadWithTeam(TournamentStandingRead):
    team: TeamReadShort