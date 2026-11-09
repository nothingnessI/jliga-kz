import os
os.environ["DB_HOST"] = os.environ.get("TEST_DB_HOST", "localhost")
os.environ["DB_PORT"] = os.environ.get("TEST_DB_PORT", "5432")
os.environ["DB_NAME"] = "jliga_test"

import asyncio
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.pool import NullPool

from app.core.config import settings
from app.core.database import Base
from app.core.security import get_password_hash
from app.db.session import get_db
from app.main import app as fastapi_app
from app.models.enums import PlayerPosition, TournamentStatus, UserRole, MatchStatus
from app.models.player import Player
from app.models.team import Team
from app.models.team_player import TeamPlayer
from app.models.tournament import Tournament
from app.models.tournament_team import TournamentTeam
from app.models.match import Match
from app.models.user import User
import app.models

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "adminpass123"

TEST_DB_URL = f"postgresql+asyncpg://{settings.DB_USER}:{settings.DB_PASSWORD}@{settings.DB_HOST}:{settings.DB_PORT}/{settings.DB_NAME}"

def _reset_schema_sync():
    async def _run():
        eng = create_async_engine(TEST_DB_URL, echo=False, poolclass=NullPool)
        try:
            async with eng.begin() as conn:
                await conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
                await conn.execute(text("CREATE SCHEMA public"))
                await conn.run_sync(Base.metadata.create_all)
        finally:
            await eng.dispose()
    asyncio.run(_run())

@pytest.fixture(scope="session", autouse=True)
def _init_schema():
    _reset_schema_sync()
    yield

@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(TEST_DB_URL, echo=False, poolclass=NullPool)
    yield eng
    await eng.dispose()

@pytest_asyncio.fixture(autouse=True)
async def _truncate_between_tests(engine):
    async with engine.begin() as conn:
        rows = await conn.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))
        tables = [r[0] for r in rows.all() if r[0] != "alembic_version"]
        if tables:
            quoted = ", ".join(f'"public"."{t}"' for t in tables)
            await conn.execute(text(f"TRUNCATE {quoted} RESTART IDENTITY CASCADE"))
    yield

@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

@pytest_asyncio.fixture
async def db(session_factory):
    async with session_factory() as session:
        yield session

@pytest_asyncio.fixture
async def client(session_factory):
    async def override_get_db():
        async with session_factory() as session:
            yield session
    fastapi_app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    fastapi_app.dependency_overrides.clear()

@pytest_asyncio.fixture
async def admin_user(db):
    user = User(
        email=ADMIN_EMAIL, hashed_password=get_password_hash(ADMIN_PASSWORD),
        first_name="Admin", last_name="Test", role=UserRole.ADMIN, is_active=True
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user

@pytest_asyncio.fixture
async def admin_client(client, admin_user):
    resp = await client.post("/web-login", data={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD}, follow_redirects=False)
    assert resp.status_code == 303
    yield client

@pytest_asyncio.fixture
async def seed_basic(db):
    teams = [Team(name=f"Team {c}", slug=f"team-{c.lower()}", is_active=True) for c in "ABCD"]
    db.add_all(teams)
    await db.flush()

    tournament = Tournament(
        name="Test League", division="A1", season="2024", age_category="ADULTS",
        status=TournamentStatus.ACTIVE, max_teams=8, logo_url="/static/test.png"
    )
    db.add(tournament)
    await db.flush()

    for team in teams:
        db.add(TournamentTeam(tournament_id=tournament.id, team_id=team.id))

    players = []
    for i, team in enumerate(teams):
        p = Player(first_name=f"First{i}", last_name=f"Last{i}", position=PlayerPosition.FORWARD)
        db.add(p)
        await db.flush()
        db.add(TeamPlayer(player_id=p.id, team_id=team.id, tournament_id=tournament.id, jersey_number=i + 1, is_active=True))
        players.append(p)

    match = Match(
        tournament_id=tournament.id, home_team_id=teams[0].id, away_team_id=teams[1].id,
        status=MatchStatus.SCHEDULED, match_date=None
    )
    db.add(match)
    await db.commit()

    for obj in [*teams, tournament, *players, match]:
        await db.refresh(obj)

    return {"teams": teams, "tournament": tournament, "players": players, "match": match}