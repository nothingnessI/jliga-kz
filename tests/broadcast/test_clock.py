
from datetime import datetime, timedelta, timezone

from app.services.broadcast import (
    MAX_CLOCK_MS,
    elapsed_now,
    state_response,
)


def test_stopped_clock(broadcast_session):
    broadcast_session.elapsed_ms = 45_000
    broadcast_session.clock_running = False

    assert elapsed_now(broadcast_session) == 45_000


def test_running_clock(broadcast_session):
    now = datetime.now(timezone.utc)

    broadcast_session.elapsed_ms = 60_000
    broadcast_session.clock_running = True
    broadcast_session.clock_started_at = (
        now - timedelta(seconds=30)
    )

    result = elapsed_now(broadcast_session, now)

    assert result == 90_000


def test_clock_does_not_count_backwards(broadcast_session):
    now = datetime.now(timezone.utc)

    broadcast_session.elapsed_ms = 120_000
    broadcast_session.clock_running = True
    broadcast_session.clock_started_at = (
        now + timedelta(seconds=10)
    )

    assert elapsed_now(broadcast_session, now) == 120_000


def test_clock_maximum(broadcast_session):
    now = datetime.now(timezone.utc)

    broadcast_session.elapsed_ms = MAX_CLOCK_MS - 1000
    broadcast_session.clock_running = True
    broadcast_session.clock_started_at = (
        now - timedelta(seconds=30)
    )

    assert elapsed_now(broadcast_session, now) == MAX_CLOCK_MS


def test_state_response_contains_current_time(broadcast_session):
    broadcast_session.elapsed_ms = 72_000

    response = state_response(broadcast_session)

    assert response.match_id == 100
    assert response.current_elapsed_ms == 72_000
    assert response.clock_running is False
    assert response.revision == 0
    assert response.server_time.tzinfo is not None
