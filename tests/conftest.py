
import asyncio
import os
import uuid

from pathlib import Path

import pytest
import pytest_asyncio

from dotenv import load_dotenv

from httpx import AsyncClient, ASGITransport

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


# ============================================================
# TEST ENVIRONMENT
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]

load_dotenv(
    PROJECT_ROOT / ".env.test",
    override=True,
)

# Важно: переменные окружения должны быть установлены
# до импорта настроек приложения.
os.environ["DB_HOST"] = os.environ.get(
    "TEST_DB_HOST",
    "localhost",
)
os.environ["DB_PORT"] = os.environ.get(
    "TEST_DB_PORT",
    "5432",
)
os.environ["DB_NAME"] = "jliga_test"


# ============================================================
# PROJECT IMPORTS
# ============================================================

from app.core.config import settings
from app.core.database import Base
from app.core.security import get_password_hash

from app.db.session import get_db
from app.main import app as fastapi_app

from app.models.enums import (
    PlayerPosition,
    TournamentStatus,
    UserRole,
    MatchStatus,
)

from app.models.player import Player
from app.models.team import Team
from app.models.team_player import TeamPlayer
from app.models.tournament import Tournament
from app.models.tournament_team import TournamentTeam
from app.models.match import Match
from app.models.user import User

from app.models.registration import (
    Registration,
    RegistrationPlayer,
    RegistrationStatus,
)

import app.models


# ============================================================
# CONSTANTS
# ============================================================

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "adminpass123"

COACH_PASSWORD = "CoachPassword123!"

TEST_DB_URL = (
    "postgresql+asyncpg://"
    f"{settings.DB_USER}:"
    f"{settings.DB_PASSWORD}@"
    f"{settings.DB_HOST}:"
    f"{settings.DB_PORT}/"
    f"{settings.DB_NAME}"
)


# ============================================================
# DATABASE SAFETY
# ============================================================

def validate_test_database():
    """
    Запрещает тестам использовать любую БД,
    кроме jliga_test.
    """

    database_name = make_url(
        TEST_DB_URL
    ).database

    if database_name != "jliga_test":
        raise RuntimeError(
            "ОПАСНО: тесты пытаются использовать "
            f"базу {database_name!r}. "
            "Разрешена только jliga_test."
        )


validate_test_database()


async def _verify_database_identity(connection):
    """
    Дополнительно проверяем имя базы на стороне PostgreSQL.
    """

    database_name = await connection.scalar(
        text("SELECT current_database()")
    )

    if database_name != "jliga_test":
        raise RuntimeError(
            "Подключение не к тестовой базе: "
            f"{database_name}"
        )


# ============================================================
# DATABASE SCHEMA INITIALIZATION
# ============================================================

def _reset_schema_sync():
    """
    Пересоздаёт public только в jliga_test.

    Используем для изолированной тестовой БД.
    Не запускать на БД с важными данными.
    """

    async def _run():
        engine = create_async_engine(
            TEST_DB_URL,
            echo=False,
            poolclass=NullPool,
        )

        try:
            async with engine.begin() as connection:

                await _verify_database_identity(
                    connection
                )

                await connection.execute(
                    text(
                        "DROP SCHEMA IF EXISTS public CASCADE"
                    )
                )

                await connection.execute(
                    text("CREATE SCHEMA public")
                )

                await connection.run_sync(
                    Base.metadata.create_all
                )

        finally:
            await engine.dispose()

    asyncio.run(_run())


@pytest.fixture(
    scope="session",
    autouse=True,
)
def _init_schema():
    """
    Один раз пересоздаём схему перед тестовой сессией.
    """

    _reset_schema_sync()
    yield


# ============================================================
# DATABASE ENGINE
# ============================================================

@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=NullPool,
    )

    try:
        yield engine
    finally:
        await engine.dispose()


# ============================================================
# DATABASE CLEANUP
# ============================================================

@pytest_asyncio.fixture(autouse=True)
async def _truncate_between_tests(engine):
    """
    Очищает таблицы перед каждым тестом.

    Существующие тесты получают чистую БД.
    """

    async with engine.begin() as connection:

        await _verify_database_identity(
            connection
        )

        result = await connection.execute(
            text(
                "SELECT tablename "
                "FROM pg_tables "
                "WHERE schemaname = 'public'"
            )
        )

        tables = [
            row[0]
            for row in result.all()
            if row[0] != "alembic_version"
        ]

        if tables:
            # Имена получены из каталога PostgreSQL.
            quoted_tables = ", ".join(
                '"public"."'
                + table.replace('"', '""')
                + '"'
                for table in tables
            )

            await connection.execute(
                text(
                    f"TRUNCATE TABLE {quoted_tables} "
                    "RESTART IDENTITY CASCADE"
                )
            )

    yield


# ============================================================
# SESSION FACTORY
# ============================================================

@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )


# ============================================================
# DATABASE SESSION
# ============================================================

@pytest_asyncio.fixture
async def db(session_factory):
    async with session_factory() as session:
        try:
            yield session
        finally:
            await session.rollback()


# ============================================================
# HTTP CLIENT
# ============================================================

@pytest_asyncio.fixture
async def client(session_factory):
    """
    HTTP-клиент для тестирования FastAPI.
    """

    async def override_get_db():
        async with session_factory() as session:
            yield session

    previous_overrides = dict(
        fastapi_app.dependency_overrides
    )

    fastapi_app.dependency_overrides[get_db] = (
        override_get_db
    )

    transport = ASGITransport(
        app=fastapi_app,
    )

    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://test",
            follow_redirects=False,
        ) as http_client:
            yield http_client

    finally:
        fastapi_app.dependency_overrides.clear()
        fastapi_app.dependency_overrides.update(
            previous_overrides
        )


# ============================================================
# ADMIN USER
# ============================================================

@pytest_asyncio.fixture
async def admin_user(db):
    user = User(
        email=ADMIN_EMAIL,
        hashed_password=get_password_hash(
            ADMIN_PASSWORD
        ),
        first_name="Admin",
        last_name="Test",
        role=UserRole.ADMIN,
        is_active=True,
    )

    db.add(user)

    await db.commit()
    await db.refresh(user)

    return user


# ============================================================
# ADMIN CLIENT
# ============================================================

@pytest_asyncio.fixture
async def admin_client(client, admin_user):

    response = await client.post(
        "/web-login",
        data={
            "username": ADMIN_EMAIL,
            "password": ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303, (
        response.text
    )

    yield client


# ============================================================
# COACH DATA
# ============================================================

@pytest.fixture
def coach_credentials():
    """
    Уникальный номер для каждого теста.
    """

    unique_digits = str(
        uuid.uuid4().int % 10**7
    ).zfill(7)

    return {
        "phone": f"+7999{unique_digits}",
        "password": COACH_PASSWORD,
        "first_name": "Александр",
        "last_name": "Тестовый",
    }


# ============================================================
# COACH REGISTRATION
# ============================================================

@pytest_asyncio.fixture
async def coach_user(
    client,
    db,
    coach_credentials,
):
    response = await client.post(
        "/api/v1/coach/register",
        json=coach_credentials,
    )

    assert response.status_code == 201, (
        response.text
    )

    coach_id = response.json()["coach_id"]

    coach = await db.get(
        User,
        coach_id,
    )

    assert coach is not None
    assert coach.role == UserRole.COACH

    return coach


# ============================================================
# AUTHENTICATED COACH CLIENT
# ============================================================

@pytest_asyncio.fixture
async def coach_client(
    client,
    coach_user,
    coach_credentials,
):
    response = await client.post(
        "/api/v1/coach/login",
        json={
            "phone": coach_credentials["phone"],
            "password": coach_credentials["password"],
        },
    )

    assert response.status_code == 200, (
        response.text
    )

    access_token = response.json()[
        "access_token"
    ]

    client.headers.update({
        "Authorization": f"Bearer {access_token}"
    })

    yield client

    client.headers.pop(
        "Authorization",
        None,
    )


# ============================================================
# BASIC SEED
# ============================================================

@pytest_asyncio.fixture
async def seed_basic(db):
    """
    Четыре команды, один турнир,
    четыре игрока и один матч.

    Сохраняем структуру твоей старой фикстуры.
    """

    teams = [
        Team(
            name=f"Team {letter}",
            slug=f"team-{letter.lower()}",
            is_active=True,
        )
        for letter in "ABCD"
    ]

    db.add_all(teams)
    await db.flush()

    tournament = Tournament(
        name="Test League",
        division="A1",
        season="2024",
        age_category="ADULTS",
        status=TournamentStatus.ACTIVE,
        max_teams=8,
        logo_url="/static/test.png",
    )

    db.add(tournament)
    await db.flush()

    for team in teams:
        db.add(
            TournamentTeam(
                tournament_id=tournament.id,
                team_id=team.id,
            )
        )

    players = []

    for index, team in enumerate(teams):

        player = Player(
            first_name=f"First{index}",
            last_name=f"Last{index}",
            position=PlayerPosition.FORWARD,
        )

        db.add(player)
        await db.flush()

        db.add(
            TeamPlayer(
                player_id=player.id,
                team_id=team.id,
                tournament_id=tournament.id,
                jersey_number=index + 1,
                is_active=True,
            )
        )

        players.append(player)

    match = Match(
        tournament_id=tournament.id,
        home_team_id=teams[0].id,
        away_team_id=teams[1].id,
        status=MatchStatus.SCHEDULED,
        match_date=None,
    )

    db.add(match)
    await db.commit()

    for obj in [
        *teams,
        tournament,
        *players,
        match,
    ]:
        await db.refresh(obj)

    return {
        "teams": teams,
        "tournament": tournament,
        "players": players,
        "match": match,
    }


# ============================================================
# APPLICATION PAYLOAD
# ============================================================

@pytest.fixture
def application_payload(seed_basic):
    """
    Готовая заявка для API-тестов.
    """

    team = seed_basic["teams"][0]
    tournament = seed_basic["tournament"]

    return {
        "team_id": team.id,
        "tournament_id": tournament.id,
        "coach": {
            "first_name": "Александр",
            "last_name": "Иванов",
            "birth_year": 1985,
            "photo_url": "/static/test/coach.jpg",
        },
        "players": [
            {
                "existing_player_id": None,
                "first_name": "Иван",
                "last_name": "Петров",
                "birth_date": "2005-04-12",
                "position": "Нападающий",
                "jersey_number": 9,
                "photo_url": "/static/test/player.jpg",
            }
        ],
    }
