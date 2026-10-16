from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import MatchEventType
from app.schemas.base import ORMModel
from app.schemas.player import PlayerReadShort


class MatchEventBase(BaseModel):
    match_id: int
    player_id: int | None = None
    team_id: int
    event_type: MatchEventType
    minute: int = Field(..., ge=0, le=120)
    comment: str | None = Field(None, max_length=300)


class MatchEventCreate(MatchEventBase):
    pass


class MatchEventUpdate(BaseModel):
    player_id: int | None = None
    team_id: int | None = None
    event_type: MatchEventType | None = None
    minute: int | None = Field(None, ge=0, le=120)
    comment: str | None = Field(None, max_length=300)


class MatchEventRead(ORMModel):
    id: int
    match_id: int
    player_id: int | None
    team_id: int
    event_type: MatchEventType
    minute: int
    comment: str | None
    created_at: datetime
    updated_at: datetime


class MatchEventReadWithPlayer(MatchEventRead):
    player: PlayerReadShort | None