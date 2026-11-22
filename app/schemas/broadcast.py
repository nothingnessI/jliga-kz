
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.models.broadcast import (
    BroadcastGraphicType,
    BroadcastPhase,
)


class BroadcastSessionCreate(BaseModel):
    match_id: int = Field(gt=0)


class BroadcastScoreUpdate(BaseModel):
    home_score: int = Field(ge=0, le=99)
    away_score: int = Field(ge=0, le=99)


class BroadcastClockSet(BaseModel):
    elapsed_ms: int = Field(
        ge=0,
        le=180 * 60 * 1000,
    )


class BroadcastAddedTimeSet(BaseModel):
    minutes: int = Field(ge=0, le=60)


class BroadcastPhaseSet(BaseModel):
    phase: BroadcastPhase


class BroadcastVisibilitySet(BaseModel):
    visible: bool


class BroadcastGraphicShow(BaseModel):
    graphic_type: BroadcastGraphicType
    payload: dict[str, Any] = Field(default_factory=dict)


class BroadcastCommandRequest(BaseModel):
    action: Literal[
        "START_CLOCK",
        "PAUSE_CLOCK",
        "RESET_CLOCK",
        "SET_CLOCK",
        "SET_SCORE",
        "SET_PHASE",
        "SET_ADDED_TIME",
        "SHOW_SCOREBOARD",
        "HIDE_SCOREBOARD",
        "SHOW_GRAPHIC",
        "HIDE_GRAPHIC",
        "ACTIVATE",
        "DEACTIVATE",
    ]

    expected_revision: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class BroadcastStateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    match_id: int
    is_active: bool
    phase: str

    home_score: int
    away_score: int

    home_team_name: str | None = None
    away_team_name: str | None = None
    home_team_short_name: str | None = None
    away_team_short_name: str | None = None
    home_team_logo_url: str | None = None
    away_team_logo_url: str | None = None

    elapsed_ms: int
    clock_running: bool
    clock_started_at: datetime | None

    added_time_minutes: int
    scoreboard_visible: bool

    active_graphic: str
    graphic_payload: dict[str, Any]

    revision: int
    server_time: datetime
    current_elapsed_ms: int


class BroadcastCommandResponse(BaseModel):
    id: int
    session_id: int
    revision: int
    command_type: str
    created_at: datetime
