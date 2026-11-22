
import asyncio
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import jwt
import pytest
import pytest_asyncio

from httpx import ASGITransport, AsyncClient

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool


# ============================================================
# 1. TEST CONFIGURATION
# ============================================================

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TEST_DB_URL = (
    "postgresql+asyncpg://"
    "postgres:1234@localhost:5432/jliga_test"
)

TEST_DB_NAME = "jliga_test"

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "adminpass123"

COACH_PASSWORD = "CoachPassword123!"


# Важно: задаём тестовую конфигурацию до импорта app.
# Это предотвращает подключение приложения к рабочей БД.

os.environ["DB_HOST"] = "localhost"
os.environ["DB_PORT"] = "5432"
os.environ["DB_NAME"] = TEST_DB_NAME
os.environ["DB_USER"] = "postgres"
os.environ["DB_PASSWORD"] = "1234"


# ============================================================
# 2. PROJECT IMPORTS
# ============================================================

from app.core.config import settings
from app.core.database import Base
from app.core.security import (
    ALGORITHM,
    get_password_hash,
)

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

from app.models.broadcast import (
    BroadcastSession,
    BroadcastPhase,
    BroadcastGraphicType,
)

# Регистрируем все ORM-модели в Base.metadata.
import app.models


# ============================================================
# 3. DATABASE SAFETY
# ============================================================

def validate_test_database():
    """
    Запрещает использовать любую БД,
    кроме выделенной jliga_test.
    """

    url = make_url(TEST_DB_URL)

    if url.drivername != "postgresql+asyncpg":
        raise RuntimeError(
            "Тесты должны использовать PostgreSQL + asyncpg."
        )

    if url.database != TEST_DB_NAME:
        raise RuntimeError(
            f"Недопустимая тестовая база: {url.database}"
        )

    if url.host not in ("localhost", "127.0.0.1"):
        raise RuntimeError(
            "Тестовая PostgreSQL должна быть локальной."
        )

    if str(settings.DB_NAME) != TEST_DB_NAME:
        raise RuntimeError(
            "Настройки приложения указывают не на jliga_test."
        )


validate_test_database()


async def verify_database_identity(connection):
    """
    Проверяем фактическую БД на сервере PostgreSQL.
    """

    actual_name = await connection.scalar(
        text("SELECT current_database()")
    )

    if actual_name != TEST_DB_NAME:
        raise RuntimeError(
            "Операция с БД запрещена: "
            f"подключение установлено к {actual_name!r}."
        )


# ============================================================
# 4. DATABASE INITIALIZATION
# ============================================================

def reset_schema_sync():
    """
    Пересоздаёт ORM-таблицы в jliga_test.

    Не удаляет схему public целиком.
    Выполняется один раз за тестовую сессию.

    ВНИМАНИЕ: данные в ORM-таблицах удаляются.
    """

    validate_test_database()

    async def run():
        engine = create_async_engine(
            TEST_DB_URL,
            echo=False,
            poolclass=NullPool,
        )

        try:
            async with engine.begin() as connection:
                await verify_database_identity(connection)

                await connection.run_sync(
                    Base.metadata.drop_all
                )

                await connection.run_sync(
                    Base.metadata.create_all
                )

        finally:
            await engine.dispose()

    asyncio.run(run())


@pytest.fixture(scope="session")
def database_schema():
    """
    Подготовка БД только для тестов,
    использующих PostgreSQL.
    """

    reset_schema_sync()
    yield


# ============================================================
# 5. ASYNC DATABASE ENGINE
# ============================================================

@pytest_asyncio.fixture
async def engine(database_schema):
    test_engine = create_async_engine(
        TEST_DB_URL,
        echo=False,
        poolclass=NullPool,
    )

    try:
        yield test_engine

    finally:
        await test_engine.dispose()


# ============================================================
# 6. CLEAN DATABASE
# ============================================================

@pytest_asyncio.fixture
async def clean_database(engine):
    """
    Очищает таблицы перед каждым DB-тестом.

    Unit-тесты без PostgreSQL не затрагиваются.
    """

    async with engine.begin() as connection:
        await verify_database_identity(connection)

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
# 7. SESSION FACTORY
# ============================================================

@pytest_asyncio.fixture
async def session_factory(engine, clean_database):
    return async_sessionmaker(
        bind=engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )


# ============================================================
# 8. DATABASE SESSION
# ============================================================

@pytest_asyncio.fixture
async def db(session_factory):
    async with session_factory() as session:
        try:
            yield session

        finally:
            await session.rollback()


# ============================================================
# 9. FASTAPI TEST CLIENT
# ============================================================

@pytest_asyncio.fixture
async def client(session_factory):
    """
    AsyncClient с подменённой зависимостью get_db.

    Все запросы работают с jliga_test.
    """

    async def override_get_db():
        async with session_factory() as session:
            try:
                yield session

            finally:
                await session.rollback()

    previous_overrides = dict(
        fastapi_app.dependency_overrides
    )

    fastapi_app.dependency_overrides[get_db] = (
        override_get_db
    )

    transport = ASGITransport(
        app=fastapi_app
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
# 10. ADMIN USER
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
# 11. ADMIN WEB CLIENT
# ============================================================

@pytest_asyncio.fixture
async def admin_client(client, admin_user):
    """
    Авторизация администратора через web-login.
    """

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
# 12. ADMIN JWT TOKEN
# ============================================================

@pytest_asyncio.fixture
async def admin_token(admin_user):
    """
    JWT администратора для REST API.
    """

    payload = {
        "sub": admin_user.email,
    }

    return jwt.encode(
        payload,
        settings.SECRET_KEY,
        algorithm=ALGORITHM,
    )


# ============================================================
# 13. ADMIN API CLIENT
# ============================================================

@pytest_asyncio.fixture
async def admin_api_client(client, admin_token):
    """
    Авторизованный API-клиент администратора.
    """

    previous_auth = client.headers.get(
        "Authorization"
    )

    client.headers["Authorization"] = (
        f"Bearer {admin_token}"
    )

    try:
        yield client

    finally:
        if previous_auth is None:
            client.headers.pop(
                "Authorization",
                None,
            )

        else:
            client.headers["Authorization"] = (
                previous_auth
            )


# ============================================================
# 14. COACH CREDENTIALS
# ============================================================

@pytest.fixture
def coach_credentials():
    """
    Уникальные данные тренера.
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
# 15. COACH USER
# ============================================================

@pytest_asyncio.fixture
async def coach_user(
    client,
    db,
    coach_credentials,
):
    """
    Регистрация тренера через API.
    """

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
# 16. COACH CLIENT
# ============================================================

@pytest_asyncio.fixture
async def coach_client(
    client,
    coach_user,
    coach_credentials,
):
    """
    Авторизованный API-клиент тренера.
    """

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

    client.headers["Authorization"] = (
        f"Bearer {access_token}"
    )

    try:
        yield client

    finally:
        client.headers.pop(
            "Authorization",
            None,
        )


# ============================================================
# 17. BASIC TEST DATA
# ============================================================

@pytest_asyncio.fixture
async def seed_basic(db):
    """
    Создаёт:
    - 4 команды
    - 1 турнир
    - 4 игроков
    - 1 матч
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
# 18. APPLICATION PAYLOAD
# ============================================================

@pytest.fixture
def application_payload(seed_basic):
    """
    Данные заявки команды.
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


# ============================================================
# 19. BROADCAST SESSION (UNIT TESTS)
# ============================================================

@pytest.fixture
def broadcast_session():
    """
    ORM-объект без обращения к PostgreSQL.

    Используется тестами:
    tests/broadcast/test_clock.py
    tests/broadcast/test_commands.py
    """

    now = datetime.now(timezone.utc)

    return BroadcastSession(
        id=1,
        match_id=100,
        is_active=True,
        phase=BroadcastPhase.FIRST_HALF.value,
        home_score=0,
        away_score=0,
        elapsed_ms=0,
        clock_running=False,
        clock_started_at=None,
        added_time_minutes=0,
        scoreboard_visible=True,
        active_graphic=BroadcastGraphicType.NONE.value,
        graphic_payload={},
        revision=0,
        created_at=now,
        updated_at=now,
    )


# ============================================================
# 20. BROADCAST DATABASE SESSION
# ============================================================

@pytest_asyncio.fixture
async def broadcast_db_session(db):
    """
    Реальная PostgreSQL-сессия для Broadcast Studio.
    """

    yield db
