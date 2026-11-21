
import uuid

import pytest
from sqlalchemy import select, func

from app.models.team import Team
from app.models.tournament import Tournament
from app.models.player import Player
from app.models.team_player import TeamPlayer
from app.models.tournament_team import TournamentTeam
from app.models.registration import (
    Registration,
    RegistrationPlayer,
)


BASE = "/api/v1/coach"


def unique_phone():
    # Номер для теста, не предназначен для SMS.
    return "+7999" + str(
        uuid.uuid4().int % 10**7
    ).zfill(7)


def application_payload(team_id, tournament_id):
    return {
        "team_id": team_id,
        "tournament_id": tournament_id,
        "coach": {
            "first_name": "Александр",
            "last_name": "Иванов",
            "birth_year": 1985,
            "photo_url": "/test/coach.jpg",
        },
        "players": [
            {
                "existing_player_id": None,
                "first_name": "Иван",
                "last_name": "Петров",
                "birth_date": "2005-04-12",
                "position": "Нападающий",
                "jersey_number": 9,
                "photo_url": "/test/player.jpg",
            }
        ],
    }


async def register_and_login(client):
    phone = unique_phone()
    password = "TestPassword123!"

    response = await client.post(
        f"{BASE}/register",
        json={
            "phone": phone,
            "password": password,
            "first_name": "Тест",
            "last_name": "Тренер",
        },
    )

    assert response.status_code == 201, response.text

    response = await client.post(
        f"{BASE}/login",
        json={
            "phone": phone,
            "password": password,
        },
    )

    assert response.status_code == 200, response.text

    token = response.json()["access_token"]

    return {
        "Authorization": f"Bearer {token}"
    }


@pytest.fixture
async def sample_team_and_tournament(db):
    suffix = uuid.uuid4().hex[:10]

    team = Team(
        name=f"Test Team {suffix}",
        slug=f"test-team-{suffix}",
        is_active=True,
    )

    tournament = Tournament(
        name=f"Test Tournament {suffix}",
        season="2026",
        division="A",
        age_category="U21",
        max_teams=16,
    )

    db.add_all([team, tournament])
    await db.commit()

    return team.id, tournament.id


async def count_rows(db, model):
    result = await db.scalar(
        select(func.count()).select_from(model)
    )
    return result


@pytest.mark.asyncio
async def test_coach_register_and_login(client):
    headers = await register_and_login(client)

    response = await client.get(
        f"{BASE}/me",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["role"] == "coach"


@pytest.mark.asyncio
async def test_wrong_password(client):
    phone = unique_phone()

    response = await client.post(
        f"{BASE}/register",
        json={
            "phone": phone,
            "password": "CorrectPassword123!",
            "first_name": "Иван",
            "last_name": "Иванов",
        },
    )

    assert response.status_code == 201, response.text

    response = await client.post(
        f"{BASE}/login",
        json={
            "phone": phone,
            "password": "WrongPassword123!",
        },
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_unauthorized_access(client):
    response = await client.get(
        f"{BASE}/applications"
    )

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_create_application(
    client,
    db,
    sample_team_and_tournament,
):
    team_id, tournament_id = sample_team_and_tournament

    headers = await register_and_login(client)

    payload = application_payload(
        team_id,
        tournament_id,
    )

    response = await client.post(
        f"{BASE}/applications",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 201, response.text

    data = response.json()

    assert data["status"] == "draft"

    application_id = data["id"]

    application = await db.get(
        Registration,
        application_id,
    )

    assert application is not None
    assert application.team_id == team_id
    assert application.tournament_id == tournament_id

    players = (
        await db.scalars(
            select(RegistrationPlayer).where(
                RegistrationPlayer.registration_id
                == application_id
            )
        )
    ).all()

    assert len(players) == 1
    assert players[0].jersey_number == 9


@pytest.mark.asyncio
async def test_coach_cannot_access_other_application(
    client,
    sample_team_and_tournament,
):
    team_id, tournament_id = sample_team_and_tournament

    coach_a = await register_and_login(client)
    coach_b = await register_and_login(client)

    response = await client.post(
        f"{BASE}/applications",
        json=application_payload(
            team_id,
            tournament_id,
        ),
        headers=coach_a,
    )

    assert response.status_code == 201, response.text

    application_id = response.json()["id"]

    response = await client.get(
        f"{BASE}/applications/{application_id}",
        headers=coach_b,
    )

    assert response.status_code == 404

    response = await client.put(
        f"{BASE}/applications/{application_id}",
        json=application_payload(
            team_id,
            tournament_id,
        ),
        headers=coach_b,
    )

    assert response.status_code == 404

    response = await client.post(
        f"{BASE}/applications/{application_id}/submit",
        headers=coach_b,
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_submit_and_lock_application(
    client,
    sample_team_and_tournament,
):
    team_id, tournament_id = sample_team_and_tournament

    headers = await register_and_login(client)

    payload = application_payload(
        team_id,
        tournament_id,
    )

    response = await client.post(
        f"{BASE}/applications",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 201, response.text

    application_id = response.json()["id"]

    response = await client.post(
        f"{BASE}/applications/{application_id}/submit",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "submitted"

    response = await client.put(
        f"{BASE}/applications/{application_id}",
        json=payload,
        headers=headers,
    )

    assert response.status_code == 409

    response = await client.post(
        f"{BASE}/applications/{application_id}/submit",
        headers=headers,
    )

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_application_does_not_change_official_data(
    client,
    db,
    sample_team_and_tournament,
):
    team_id, tournament_id = sample_team_and_tournament

    before = {
        "players": await count_rows(db, Player),
        "team_players": await count_rows(db, TeamPlayer),
        "tournament_teams": await count_rows(
            db, TournamentTeam
        ),
    }

    headers = await register_and_login(client)

    response = await client.post(
        f"{BASE}/applications",
        json=application_payload(
            team_id,
            tournament_id,
        ),
        headers=headers,
    )

    assert response.status_code == 201, response.text

    application_id = response.json()["id"]

    response = await client.post(
        f"{BASE}/applications/{application_id}/submit",
        headers=headers,
    )

    assert response.status_code == 200, response.text

    after = {
        "players": await count_rows(db, Player),
        "team_players": await count_rows(db, TeamPlayer),
        "tournament_teams": await count_rows(
            db, TournamentTeam
        ),
    }

    assert before == after, (
        "Подача заявки изменила официальные таблицы!"
    )
