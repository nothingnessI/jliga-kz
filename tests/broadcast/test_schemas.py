
import pytest
from pydantic import ValidationError

from app.schemas.broadcast import (
    BroadcastClockSet,
    BroadcastCommandRequest,
    BroadcastScoreUpdate,
)


def test_valid_score():
    score = BroadcastScoreUpdate(
        home_score=2,
        away_score=5,
    )

    assert score.home_score == 2
    assert score.away_score == 5


@pytest.mark.parametrize(
    "home, away",
    [
        (-1, 0),
        (0, -1),
        (100, 0),
        (0, 100),
    ],
)
def test_invalid_score(home, away):
    with pytest.raises(ValidationError):
        BroadcastScoreUpdate(
            home_score=home,
            away_score=away,
        )


def test_negative_clock_rejected():
    with pytest.raises(ValidationError):
        BroadcastClockSet(elapsed_ms=-1000)


def test_invalid_command_rejected():
    with pytest.raises(ValidationError):
        BroadcastCommandRequest(
            action="DELETE_MATCH",
            expected_revision=0,
        )


def test_negative_revision_rejected():
    with pytest.raises(ValidationError):
        BroadcastCommandRequest(
            action="SET_SCORE",
            expected_revision=-1,
            payload={
                "home_score": 1,
                "away_score": 0,
            },
        )
