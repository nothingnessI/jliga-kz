
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from app.models.broadcast import BroadcastCommand
from app.schemas.broadcast import BroadcastCommandRequest
from app.services.broadcast import BroadcastService


@pytest.fixture
def mock_db(broadcast_session):
    db = MagicMock()
    db.scalar = AsyncMock(return_value=broadcast_session)
    db.flush = AsyncMock()
    db.add = MagicMock()
    return db


async def execute(
    db,
    action,
    revision,
    payload=None,
):
    return await BroadcastService.execute_command(
        db=db,
        session_id=1,
        command=BroadcastCommandRequest(
            action=action,
            expected_revision=revision,
            payload=payload or {},
        ),
        operator_id=None,
    )


@pytest.mark.asyncio
async def test_set_score(mock_db, broadcast_session):
    result = await execute(
        mock_db,
        "SET_SCORE",
        0,
        {
            "home_score": 2,
            "away_score": 5,
        },
    )

    assert result.home_score == 2
    assert result.away_score == 5
    assert result.revision == 1

    assert broadcast_session.home_score == 2
    assert broadcast_session.away_score == 5

    mock_db.flush.assert_awaited_once()

    log = mock_db.add.call_args.args[0]
    assert isinstance(log, BroadcastCommand)
    assert log.command_type == "SET_SCORE"
    assert log.revision == 1


@pytest.mark.asyncio
async def test_revision_conflict(mock_db, broadcast_session):
    broadcast_session.revision = 5

    with pytest.raises(HTTPException) as error:
        await execute(
            mock_db,
            "SET_SCORE",
            4,
            {
                "home_score": 3,
                "away_score": 1,
            },
        )

    assert error.value.status_code == 409
    assert broadcast_session.home_score == 0
    assert broadcast_session.away_score == 0

    mock_db.add.assert_not_called()
    mock_db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_and_pause_clock(
    mock_db,
    broadcast_session,
):
    started = await execute(
        mock_db,
        "START_CLOCK",
        0,
    )

    assert started.clock_running is True
    assert broadcast_session.clock_started_at is not None

    paused = await execute(
        mock_db,
        "PAUSE_CLOCK",
        1,
    )

    assert paused.clock_running is False
    assert paused.clock_started_at is None
    assert paused.revision == 2


@pytest.mark.asyncio
async def test_show_and_hide_graphic(
    mock_db,
    broadcast_session,
):
    shown = await execute(
        mock_db,
        "SHOW_GRAPHIC",
        0,
        {
            "graphic_type": "GOAL",
            "payload": {
                "team_id": 10,
                "player_id": 25,
                "minute": 72,
            },
        },
    )

    assert shown.active_graphic == "GOAL"
    assert shown.graphic_payload["minute"] == 72

    hidden = await execute(
        mock_db,
        "HIDE_GRAPHIC",
        1,
    )

    assert hidden.active_graphic == "NONE"
    assert hidden.graphic_payload == {}


@pytest.mark.asyncio
async def test_invalid_score_does_not_change_state(
    mock_db,
    broadcast_session,
):
    with pytest.raises(HTTPException) as error:
        await execute(
            mock_db,
            "SET_SCORE",
            0,
            {
                "home_score": -1,
                "away_score": 2,
            },
        )

    assert error.value.status_code == 422
    assert broadcast_session.revision == 0
    assert broadcast_session.home_score == 0

    mock_db.add.assert_not_called()


@pytest.mark.asyncio
async def test_finish_match_stops_clock(
    mock_db,
    broadcast_session,
):
    await execute(
        mock_db,
        "START_CLOCK",
        0,
    )

    result = await execute(
        mock_db,
        "SET_PHASE",
        1,
        {"phase": "FINISHED"},
    )

    assert result.phase == "FINISHED"
    assert result.clock_running is False
    assert result.clock_started_at is None

    with pytest.raises(HTTPException) as error:
        await execute(
            mock_db,
            "START_CLOCK",
            2,
        )

    assert error.value.status_code == 409


@pytest.mark.asyncio
async def test_deactivate_broadcast(
    mock_db,
    broadcast_session,
):
    broadcast_session.active_graphic = "GOAL"
    broadcast_session.graphic_payload = {
        "player_id": 25,
    }

    result = await execute(
        mock_db,
        "DEACTIVATE",
        0,
    )

    assert result.is_active is False
    assert result.clock_running is False
    assert result.active_graphic == "NONE"
    assert result.graphic_payload == {}
