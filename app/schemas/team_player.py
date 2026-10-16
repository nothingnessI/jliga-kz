from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.base import ORMModel
from app.schemas.player import PlayerReadShort


class TeamPlayerBase(BaseModel):
    player_id: int
    team_id: int
    tournament_id: int
    jersey_number: int | None = Field(None, ge=1, le=99)
    is_captain: bool = False
    is_active: bool = True


class TeamPlayerCreate(TeamPlayerBase):
    pass


class TeamPlayerUpdate(BaseModel):
    jersey_number: int | None = Field(None, ge=1, le=99)
    is_captain: bool | None = None
    is_active: bool | None = None


class TeamPlayerRead(ORMModel):
    id: int
    player_id: int
    team_id: int
    tournament_id: int
    jersey_number: int | None
    is_captain: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime


class TeamPlayerReadWithPlayer(TeamPlayerRead):
    player: PlayerReadShort