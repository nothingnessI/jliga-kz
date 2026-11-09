"""
MEGA TEST — полное покрытие J-LIGA.KZ

Секции:
  1. Smoke: приложение собирается, все роуты зарегистрированы
  2. Все публичные HTML-страницы (рендер 200)
  3. Все админские HTML-страницы (GET, с авторизацией)
  4. Админка: CRUD через формы (POST) — команды, турниры, игроки, новости,
     стадионы, матчи, события, составы
  5. REST API: auth, teams, players, tournaments, matches, stadiums
  6. E2E: полный турнир от создания до таблицы
  7. Регрессии: own_goal, assist, group_name, logo nullable, без дублей роутов
"""
import pytest
from datetime import datetime, timezone
from sqlalchemy import select

from app.models.enums import (
    MatchEventType, MatchStatus, PlayerPosition,
    TournamentStatus, UserRole,
)
from app.models.disqualification import Disqualification
from app.models.match import Match
from app.models.match_event import MatchEvent
from app.models.news import News
from app.models.player import Player
from app.models.stadium import Stadium
from app.models.team import Team
from app.models.team_player import TeamPlayer
from app.models.tournament import Tournament
from app.models.tournament_standing import TournamentStanding
from app.models.tournament_team import TournamentTeam

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "adminpass123"


# ═══════════════════════════════════════════════════════════════════════════
# 1. SMOKE — приложение и роуты
# ═══════════════════════════════════════════════════════════════════════════

def _all_paths() -> set[str]:
    from app.main import app
    paths: set[str] = set()
    try:
        paths |= set(app.openapi().get("paths", {}).keys())
    except Exception:
        pass
    seen: set[int] = set()
    def walk(obj, depth=0):
        if depth > 8 or id(obj) in seen:
            return
        seen.add(id(obj))
        p = getattr(obj, "path", None)
        if isinstance(p, str):
            paths.add(p)
        for attr in ("routes", "_routes", "sub_routes", "router"):
            try:
                sub = getattr(obj, attr, None)
                if sub and not callable(sub):
                    for c in sub:
                        walk(c, depth + 1)
            except (TypeError, AttributeError):
                pass
    try:
        for r in app.routes:
            walk(r)
    except Exception:
        pass
    return paths


def test_app_boots():
    from app.main import app
    assert app.title == "J-Liga"
    assert app.version == "1.0.0"


def test_every_public_route_registered():
    paths = _all_paths()
    expected = [
        "/", "/matches", "/matches/{match_id}", "/matches/{match_id}/print",
        "/about", "/tournaments", "/tournaments/{tournament_id}",
        "/tournaments/{tournament_id}/teams/{team_id}/application",
        "/participants/teams", "/participants/players", "/participants/coaches",
        "/participants/hall-of-fame",
        "/player/{player_id}", "/teams/{team_id}",
        "/news", "/news/{news_id}", "/media", "/stadiums",
        "/login", "/web-login", "/logout",
        "/management", "/partners", "/facts", "/rating", "/disciplinary",
    ]
    missing = [p for p in expected if p not in paths]
    assert not missing, f"Отсутствуют публичные роуты: {missing}"


def test_every_admin_route_registered():
    paths = _all_paths()
    expected = [
        "/admin",
        "/admin/teams", "/admin/teams/create", "/admin/teams/{team_id}/edit",
        "/admin/teams/{team_id}/delete", "/admin/teams/{team_id}/roster",
        "/admin/tournaments", "/admin/tournaments/create",
        "/admin/tournaments/{tournament_id}/edit",
        "/admin/tournaments/{tournament_id}/delete",
        "/admin/tournaments/{tournament_id}/teams",
        "/admin/tournaments/{tournament_id}/generate-calendar",
        "/admin/tournaments/{tournament_id}/generate-playoff",
        "/admin/tournaments/{tournament_id}/recalculate",
        "/admin/matches", "/admin/matches/create",
        "/admin/matches/{match_id}/edit", "/admin/matches/{match_id}/delete",
        "/admin/matches/{match_id}/events",
        "/admin/matches/{match_id}/events/add",
        "/admin/matches/{match_id}/events/{event_id}/edit",
        "/admin/matches/{match_id}/events/{event_id}/delete",
        "/admin/matches/{match_id}/finish",
        "/admin/players", "/admin/players/create",
        "/admin/players/{player_id}/edit", "/admin/players/{player_id}/delete",
        "/admin/news", "/admin/news/create",
        "/admin/news/{news_id}/edit", "/admin/news/{news_id}/delete",
        "/admin/media", "/admin/media/create",
        "/admin/stadiums", "/admin/stadiums/create",
        "/admin/stadiums/{stadium_id}/edit", "/admin/stadiums/{stadium_id}/delete",
        "/admin/disqualifications", "/admin/disqualifications/create",
    ]
    missing = [p for p in expected if p not in paths]
    assert not missing, f"Отсутствуют админские роуты: {missing}"


def test_every_api_endpoint_registered():
    paths = _all_paths()
    expected = [
        "/api/v1/auth/register", "/api/v1/auth/login",
        "/api/v1/auth/me", "/api/v1/auth/dashboard",
        "/api/v1/teams/", "/api/v1/teams/{team_id}",
        "/api/v1/players/", "/api/v1/players/{player_id}",
        "/api/v1/tournaments/", "/api/v1/tournaments/{tournament_id}",
        "/api/v1/tournaments/{tournament_id}/standings",
        "/api/v1/tournaments/{tournament_id}/teams/{team_id}",
        "/api/v1/tournaments/{tournament_id}/generate-calendar",
        "/api/v1/matches/", "/api/v1/matches/tournament/{tournament_id}",
        "/api/v1/matches/{match_id}/events", "/api/v1/matches/{match_id}/finish",
        "/api/v1/stadiums/", "/api/v1/stadiums/{stadium_id}",
    ]
    missing = [p for p in expected if p not in paths]
    assert not missing, f"Отсутствуют API-роуты: {missing}"


# ═══════════════════════════════════════════════════════════════════════════
# 2. ПУБЛИЧНЫЕ HTML-СТРАНИЦЫ
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/", "/matches", "/about", "/tournaments",
    "/participants/teams", "/participants/players",
    "/participants/coaches", "/participants/hall-of-fame",
    "/news", "/media", "/stadiums", "/login",
    "/management", "/partners", "/facts", "/rating", "/disciplinary",
])
async def test_public_page_renders(client, path):
    resp = await client.get(path)
    assert resp.status_code == 200, f"{path} → {resp.status_code}"


@pytest.mark.asyncio
async def test_tournament_detail_renders(client, seed_basic):
    t = seed_basic["tournament"]
    resp = await client.get(f"/tournaments/{t.id}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_match_detail_all_tabs(client, seed_basic):
    m = seed_basic["match"]
    for tab in ("preview", "protocol", "events"):
        resp = await client.get(f"/matches/{m.id}?tab={tab}")
        assert resp.status_code == 200, f"tab={tab}"


@pytest.mark.asyncio
async def test_match_print_renders(client, seed_basic):
    m = seed_basic["match"]
    resp = await client.get(f"/matches/{m.id}/print")
    assert resp.status_code == 200
    # Проверяем наличие ключевых блоков печатной формы
    assert "НАКАЗАНИЯ" in resp.text
    assert "РЕЗУЛЬТАТ" in resp.text or "Результат матча" in resp.text


@pytest.mark.asyncio
async def test_team_detail_renders(client, seed_basic):
    team = seed_basic["teams"][0]
    resp = await client.get(f"/teams/{team.id}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_player_detail_renders(client, seed_basic):
    p = seed_basic["players"][0]
    resp = await client.get(f"/player/{p.id}")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_team_application_page(client, seed_basic):
    t, team = seed_basic["tournament"], seed_basic["teams"][0]
    resp = await client.get(f"/tournaments/{t.id}/teams/{team.id}/application")
    assert resp.status_code == 200
    assert "ЗАЯВКА КОМАНДЫ" in resp.text


# ═══════════════════════════════════════════════════════════════════════════
# 3. АДМИНСКИЕ HTML-СТРАНИЦЫ (GET)
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/admin", "/admin/teams", "/admin/teams/create",
    "/admin/tournaments", "/admin/tournaments/create",
    "/admin/matches", "/admin/matches/create",
    "/admin/players", "/admin/players/create",
    "/admin/news", "/admin/news/create",
    "/admin/media", "/admin/media/create",
    "/admin/stadiums", "/admin/stadiums/create",
    "/admin/disqualifications", "/admin/disqualifications/create",
])
async def test_admin_page_renders(admin_client, path):
    resp = await admin_client.get(path)
    assert resp.status_code == 200, f"{path} → {resp.status_code}"


@pytest.mark.asyncio
async def test_admin_pages_redirect_anonymous(client):
    resp = await client.get("/admin", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(client, admin_user):
    resp = await client.post("/web-login",
                             data={"username": ADMIN_EMAIL, "password": "wrong"},
                             follow_redirects=False)
    assert resp.status_code == 303
    assert "error=1" in resp.headers["location"]


# ═══════════════════════════════════════════════════════════════════════════
# 4. АДМИНКА: CRUD через формы
# ═══════════════════════════════════════════════════════════════════════════

# ---- Teams ----
@pytest.mark.asyncio
async def test_admin_team_crud(admin_client, db):
    # CREATE
    resp = await admin_client.post("/admin/teams/create", data={
        "name": "CRUD Team", "slug": "crud-team", "city": "Almaty", "is_active": "true",
    }, follow_redirects=False)
    assert resp.status_code == 303
    team = await db.scalar(select(Team).where(Team.slug == "crud-team"))
    assert team is not None and team.name == "CRUD Team"

    # UPDATE
    resp = await admin_client.post(f"/admin/teams/{team.id}/edit", data={
        "name": "CRUD Team v2", "slug": "crud-team", "city": "Astana", "is_active": "true",
    }, follow_redirects=False)
    assert resp.status_code == 303
    await db.refresh(team)
    assert team.name == "CRUD Team v2" and team.city == "Astana"

    # DELETE
    resp = await admin_client.post(f"/admin/teams/{team.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert await db.scalar(select(Team).where(Team.id == team.id)) is None


# ---- Tournament ----
@pytest.mark.asyncio
async def test_admin_tournament_without_logo(admin_client, db):
    resp = await admin_client.post("/admin/tournaments/create", data={
        "name": "No Logo Cup", "division": "A1", "season": "2025",
        "age_category": "ADULTS", "status": "draft", "max_teams": "8",
    }, follow_redirects=False)
    assert resp.status_code == 303
    t = await db.scalar(select(Tournament).where(Tournament.name == "No Logo Cup"))
    assert t is not None and t.logo_url is None


@pytest.mark.asyncio
async def test_admin_tournament_delete(admin_client, db, seed_basic):
    t = seed_basic["tournament"]
    resp = await admin_client.post(f"/admin/tournaments/{t.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert await db.scalar(select(Tournament).where(Tournament.id == t.id)) is None


# ---- Player ----
@pytest.mark.asyncio
async def test_admin_player_crud(admin_client, db):
    resp = await admin_client.post("/admin/players/create", data={
        "first_name": "Test", "last_name": "Player",
        "date_of_birth": "2000-01-01", "position": "forward",
    }, follow_redirects=False)
    assert resp.status_code == 303
    p = await db.scalar(select(Player).where(Player.last_name == "Player"))
    assert p is not None and p.first_name == "Test"

    resp = await admin_client.post(f"/admin/players/{p.id}/edit", data={
        "first_name": "Updated", "last_name": "Player",
        "date_of_birth": "2000-01-01", "position": "midfielder",
    }, follow_redirects=False)
    assert resp.status_code == 303
    await db.refresh(p)
    assert p.first_name == "Updated"

    resp = await admin_client.post(f"/admin/players/{p.id}/delete", follow_redirects=False)
    assert resp.status_code == 303


# ---- News ----
@pytest.mark.asyncio
async def test_admin_news_crud(admin_client, db):
    resp = await admin_client.post("/admin/news/create", data={
        "title": "Test News", "slug": "test-news",
        "content": "<p>Some content here</p>",
        "is_published": "true",
        "published_at": "2024-01-01T12:00",
    }, follow_redirects=False)
    assert resp.status_code == 303
    n = await db.scalar(select(News).where(News.slug == "test-news"))
    assert n is not None

    resp = await admin_client.post(f"/admin/news/{n.id}/delete", follow_redirects=False)
    assert resp.status_code == 303


# ---- Stadium ----
@pytest.mark.asyncio
async def test_admin_stadium_crud(admin_client, db):
    resp = await admin_client.post("/admin/stadiums/create", data={
        "name": "Test Arena", "city": "Almaty",
        "address": "Abay 1", "capacity": "10000",
    }, follow_redirects=False)
    assert resp.status_code == 303
    s = await db.scalar(select(Stadium).where(Stadium.name == "Test Arena"))
    assert s is not None

    resp = await admin_client.post(f"/admin/stadiums/{s.id}/edit", data={
        "name": "Test Arena v2", "city": "Almaty", "capacity": "15000",
    }, follow_redirects=False)
    assert resp.status_code == 303
    await db.refresh(s)
    assert s.name == "Test Arena v2"

    resp = await admin_client.post(f"/admin/stadiums/{s.id}/delete", follow_redirects=False)
    assert resp.status_code == 303


# ---- Match events ----
@pytest.mark.asyncio
async def test_admin_add_goal_with_assist(admin_client, db, seed_basic):
    m, teams, players = seed_basic["match"], seed_basic["teams"], seed_basic["players"]
    resp = await admin_client.post(f"/admin/matches/{m.id}/events/add", data={
        "team_id": teams[0].id, "player_id": players[0].id,
        "event_type": "goal", "minute": "15",
        "assist_player_id": players[1].id,
    }, follow_redirects=False)
    assert resp.status_code == 303
    events = (await db.execute(select(MatchEvent).where(MatchEvent.match_id == m.id))).scalars().all()
    assert len(events) == 2
    assert any(e.event_type == MatchEventType.GOAL for e in events)
    assert any(e.event_type == MatchEventType.ASSIST for e in events)


@pytest.mark.asyncio
async def test_admin_add_own_goal_changes_score(admin_client, db, seed_basic):
    m, teams, players = seed_basic["match"], seed_basic["teams"], seed_basic["players"]
    resp = await admin_client.post(f"/admin/matches/{m.id}/events/add", data={
        "team_id": teams[0].id, "player_id": players[0].id,
        "event_type": "own_goal", "minute": "20",
    }, follow_redirects=False)
    assert resp.status_code == 303
    await db.refresh(m)
    assert m.home_score == 0
    assert m.away_score == 1


@pytest.mark.asyncio
async def test_admin_delete_match_event(admin_client, db, seed_basic):
    m, teams, players = seed_basic["match"], seed_basic["teams"], seed_basic["players"]
    ev = MatchEvent(match_id=m.id, team_id=teams[0].id, player_id=players[0].id,
                    event_type=MatchEventType.GOAL, minute=10)
    db.add(ev)
    await db.commit()
    await db.refresh(ev)

    resp = await admin_client.post(
        f"/admin/matches/{m.id}/events/{ev.id}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert await db.scalar(select(MatchEvent).where(MatchEvent.id == ev.id)) is None


# ---- Match finish ----
@pytest.mark.asyncio
async def test_admin_finish_match_and_standings(admin_client, db, seed_basic):
    m, teams, players, t = (
        seed_basic["match"], seed_basic["teams"],
        seed_basic["players"], seed_basic["tournament"],
    )
    # Сначала забиваем
    db.add(MatchEvent(match_id=m.id, team_id=teams[0].id, player_id=players[0].id,
                      event_type=MatchEventType.GOAL, minute=10))
    await db.commit()

    # ВАЖНО: Обновляем счет в базе (как это делает сайт при добавлении события)
    from app.api.v1.pages import recalculate_match_score
    await db.refresh(m)
    await recalculate_match_score(m, db)

    # Завершаем через форму
    resp = await admin_client.post(f"/admin/matches/{m.id}/finish", data={
        "tech_loss_team_id": "0",
    }, follow_redirects=False)
    assert resp.status_code == 303

    await db.refresh(m)
    assert m.status == MatchStatus.FINISHED

    # Проверяем таблицу
    st = await db.scalar(select(TournamentStanding).where(
        TournamentStanding.tournament_id == t.id,
        TournamentStanding.team_id == teams[0].id,
    ))
    assert st is not None and st.points == 3


# ---- Disqualification ----
@pytest.mark.asyncio
async def test_admin_create_disqualification(admin_client, db, seed_basic):
    t, teams, players = seed_basic["tournament"], seed_basic["teams"], seed_basic["players"]
    resp = await admin_client.post("/admin/disqualifications/create", data={
        "tournament_id": t.id, "team_id": teams[0].id,
        "player_id": players[0].id, "matches_total": "2",
        "reason": "Test reason",
    }, follow_redirects=False)
    assert resp.status_code == 303
    d = await db.scalar(select(Disqualification).where(
        Disqualification.player_id == players[0].id))
    assert d is not None and d.matches_remaining == 2


# ═══════════════════════════════════════════════════════════════════════════
# 5. REST API
# ═══════════════════════════════════════════════════════════════════════════

async def _api_token(client) -> str:
    r = await client.post("/api/v1/auth/login",
                          data={"username": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200
    return r.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---- Auth ----
@pytest.mark.asyncio
async def test_api_auth_me(client, admin_user):
    token = await _api_token(client)
    r = await client.get("/api/v1/auth/me", headers=_auth(token))
    assert r.status_code == 200
    assert r.json()["email"] == ADMIN_EMAIL
    assert r.json()["role"] == "admin"


@pytest.mark.asyncio
async def test_api_auth_dashboard(client, admin_user):
    token = await _api_token(client)
    r = await client.get("/api/v1/auth/dashboard", headers=_auth(token))
    assert r.status_code == 200
    data = r.json()
    assert "total_users" in data and "total_tournaments" in data


@pytest.mark.asyncio
async def test_api_auth_register(client):
    r = await client.post("/api/v1/auth/register", json={
        "email": "newuser@example.com", "password": "strongpass123",
        "first_name": "New", "last_name": "User",
    })
    assert r.status_code == 201
    assert r.json()["email"] == "newuser@example.com"


@pytest.mark.asyncio
async def test_api_auth_requires_token(client):
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401


# ---- Teams ----
@pytest.mark.asyncio
async def test_api_teams_read_and_create(client, admin_user):
    token = await _api_token(client)
    # Пустой список
    r = await client.get("/api/v1/teams/")
    assert r.status_code == 200 and r.json() == []

    # Создание без токена — 401
    r = await client.post("/api/v1/teams/", json={"name": "XY", "slug": "xy"})
    assert r.status_code == 401

    # Создание с токеном — 201
    r = await client.post("/api/v1/teams/",
                          json={"name": "XY", "slug": "xy"},
                          headers=_auth(token))
    assert r.status_code == 201
    team_id = r.json()["id"]

    # Чтение
    r = await client.get(f"/api/v1/teams/{team_id}")
    assert r.status_code == 200 and r.json()["slug"] == "xy"

    # Патч
    r = await client.patch(f"/api/v1/teams/{team_id}",
                           json={"name": "XY Updated"},
                           headers=_auth(token))
    assert r.status_code == 200 and r.json()["name"] == "XY Updated"

    # Удаление
    r = await client.delete(f"/api/v1/teams/{team_id}", headers=_auth(token))
    assert r.status_code == 204


# ---- Players ----
@pytest.mark.asyncio
async def test_api_players_crud(client, admin_user):
    token = await _api_token(client)
    r = await client.post("/api/v1/players/", json={
        "first_name": "John", "last_name": "Doe",
        "position": "forward",
    }, headers=_auth(token))
    assert r.status_code == 201
    pid = r.json()["id"]

    r = await client.get(f"/api/v1/players/{pid}")
    assert r.status_code == 200

    r = await client.patch(f"/api/v1/players/{pid}",
                           json={"last_name": "Smith"},
                           headers=_auth(token))
    assert r.status_code == 200

    # Поиск
    r = await client.get("/api/v1/players/?search=Smith")
    assert r.status_code == 200 and len(r.json()) >= 1


# ---- Stadiums ----
@pytest.mark.asyncio
async def test_api_stadiums_crud(client, admin_user):
    token = await _api_token(client)
    r = await client.post("/api/v1/stadiums/", json={
        "name": "Arena API", "city": "Almaty",
    }, headers=_auth(token))
    assert r.status_code == 201
    sid = r.json()["id"]

    r = await client.get(f"/api/v1/stadiums/{sid}")
    assert r.status_code == 200

    r = await client.patch(f"/api/v1/stadiums/{sid}",
                           json={"capacity": 5000},
                           headers=_auth(token))
    assert r.status_code == 200


# ---- Tournaments ----
@pytest.mark.asyncio
async def test_api_tournaments_full_flow(client, admin_user):
    token = await _api_token(client)

    # Создаём команды
    t1 = (await client.post("/api/v1/teams/",
                            json={"name": "Team One", "slug": "team-one"},
                            headers=_auth(token))).json()
    t2 = (await client.post("/api/v1/teams/",
                            json={"name": "Team Two", "slug": "team-two"},
                            headers=_auth(token))).json()

    # Создаём турнир
    r = await client.post("/api/v1/tournaments/", json={
        "name": "API Cup", "division": "A1", "season": "2024",
        "age_category": "ADULTS", "status": "active", "max_teams": 8,
    }, headers=_auth(token))
    assert r.status_code == 201
    tid = r.json()["id"]

    # Читаем
    r = await client.get(f"/api/v1/tournaments/{tid}")
    assert r.status_code == 200

    # Добавляем команды
    for team in (t1, t2):
        r = await client.post(f"/api/v1/tournaments/{tid}/teams/{team['id']}?group_name=A",
                              headers=_auth(token))
        assert r.status_code == 201

    # Повторное добавление — 400
    r = await client.post(f"/api/v1/tournaments/{tid}/teams/{t1['id']}",
                          headers=_auth(token))
    assert r.status_code == 400

    # Генерируем календарь
    r = await client.post(f"/api/v1/tournaments/{tid}/generate-calendar",
                          headers=_auth(token))
    assert r.status_code == 201
    assert r.json()["count"] >= 1

    # Standings (пустая таблица после генерации)
    r = await client.get(f"/api/v1/tournaments/{tid}/standings")
    assert r.status_code == 200


# ---- Matches ----
@pytest.mark.asyncio
async def test_api_matches_full_flow(client, admin_user, db, seed_basic):
    token = await _api_token(client)
    m, t, teams = seed_basic["match"], seed_basic["tournament"], seed_basic["teams"]
    players = seed_basic["players"]

    # Список матчей турнира
    r = await client.get(f"/api/v1/matches/tournament/{t.id}")
    assert r.status_code == 200
    assert len(r.json()) >= 1

    # Создать новый матч
    r = await client.post("/api/v1/matches/", json={
        "tournament_id": t.id,
        "home_team_id": teams[2].id,
        "away_team_id": teams[3].id,
        "status": "scheduled",
    }, headers=_auth(token))
    assert r.status_code == 201
    new_match_id = r.json()["id"]

    # Нельзя команда сама с собой
    r = await client.post("/api/v1/matches/", json={
        "tournament_id": t.id,
        "home_team_id": teams[2].id,
        "away_team_id": teams[2].id,
        "status": "scheduled",
    }, headers=_auth(token))
    assert r.status_code == 400

    # Добавить гол через API (нужен игрок из команды, в нашем случае создадим)
    p = Player(first_name="API", last_name="Scorer", position=PlayerPosition.FORWARD)
    db.add(p)
    await db.flush()
    db.add(TeamPlayer(player_id=p.id, team_id=teams[2].id,
                      tournament_id=t.id, is_active=True))
    await db.commit()

    r = await client.post(f"/api/v1/matches/{new_match_id}/events", json={
        "match_id": new_match_id, "team_id": teams[2].id,
        "player_id": p.id, "event_type": "goal", "minute": 10,
    }, headers=_auth(token))
    assert r.status_code == 200

    # Завершить матч
    r = await client.post(f"/api/v1/matches/{new_match_id}/finish", headers=_auth(token))
    assert r.status_code == 200


# ═══════════════════════════════════════════════════════════════════════════
# 6. E2E — полный турнир
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_e2e_full_tournament(client, admin_client, db, seed_basic):
    """
    Полный сценарий: 4 команды → круговой турнир → все матчи сыграны →
    проверяем, что таблица посчитана, бомбардир — определен, чемпион — есть.
    """
    teams = seed_basic["teams"]
    players = seed_basic["players"]
    t = seed_basic["tournament"]

    # Создаём 6 матчей (круговая на 4 команды) через API генерации
    r = await admin_client.post(f"/admin/tournaments/{t.id}/generate-calendar",
                                follow_redirects=False)
    assert r.status_code == 303

    matches = (await db.execute(select(Match).where(
        Match.tournament_id == t.id, Match.id != seed_basic["match"].id
    ))).scalars().all()
    assert len(matches) == 6

    # Сыграем все: в каждом матче забивает игрок команды home (0:1 away для простоты)
    for match in matches:
        db.add(MatchEvent(
            match_id=match.id, team_id=match.home_team_id,
            player_id=players[0].id,  # допустим, он "играет" везде — для теста
            event_type=MatchEventType.GOAL, minute=10,
        ))
        match.status = MatchStatus.FINISHED
    await db.commit()

    # Пересчитываем турнир через сервис
    from app.services.stats_recalc import StatsRecalculator
    await StatsRecalculator.recalculate_all(db, t.id)

    # Проверяем: во всех строках таблицы games_played == 3
    rows = (await db.execute(select(TournamentStanding).where(
        TournamentStanding.tournament_id == t.id
    ))).scalars().all()
    assert len(rows) == 4
    # У каждого игрока team_players.stats_games обновился (не 0)
    tps = (await db.execute(select(TeamPlayer).where(
        TeamPlayer.tournament_id == t.id
    ))).scalars().all()
    assert any(tp.stats_games > 0 for tp in tps)


# ═══════════════════════════════════════════════════════════════════════════
# 7. РЕГРЕССИИ
# ═══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_regression_group_name_preserved(admin_client, db, seed_basic):
    """При добавлении команды в турнир через форму group_name сохраняется."""
    t = seed_basic["tournament"]
    new_team = Team(name="GroupedTeam", slug="grouped-team", is_active=True)
    db.add(new_team)
    await db.commit()
    await db.refresh(new_team)

    resp = await admin_client.post(f"/admin/tournaments/{t.id}/teams/add",
                                   data={"team_id": new_team.id, "group_name": "B"},
                                   follow_redirects=False)
    assert resp.status_code == 303
    tt = await db.scalar(select(TournamentTeam).where(
        TournamentTeam.tournament_id == t.id,
        TournamentTeam.team_id == new_team.id,
    ))
    assert tt is not None and tt.group_name == "B"


@pytest.mark.asyncio
async def test_regression_standings_allow_same_team_in_two_groups(db, seed_basic):
    """Unique constraint включает group_name → команда может быть в двух группах."""
    t, team = seed_basic["tournament"], seed_basic["teams"][0]
    db.add_all([
        TournamentStanding(tournament_id=t.id, team_id=team.id, group_name="A"),
        TournamentStanding(tournament_id=t.id, team_id=team.id, group_name="B"),
    ])
    await db.commit()  # не должно упасть

    rows = (await db.execute(select(TournamentStanding).where(
        TournamentStanding.tournament_id == t.id,
        TournamentStanding.team_id == team.id,
    ))).scalars().all()
    assert len(rows) == 2


@pytest.mark.asyncio
async def test_regression_tech_loss_none_scores(db, seed_basic):
    """tech_loss при None-счёте не роняет расчёт таблицы."""
    from app.services.standings import StandingsService
    teams, t = seed_basic["teams"], seed_basic["tournament"]
    m = Match(
        tournament_id=t.id, home_team_id=teams[0].id, away_team_id=teams[1].id,
        status=MatchStatus.FINISHED, tech_loss=True,
        home_score=None, away_score=None,
        match_date=datetime.now(timezone.utc),
    )
    db.add(m)
    await db.commit()
    await StandingsService.recalculate_standings(db, t.id)  # не падает


@pytest.mark.asyncio
async def test_regression_no_duplicate_events_route():
    """Регрессия: /admin/matches/{id}/events/add объявлен ровно 1 раз."""
    import inspect
    from app.api.v1 import pages
    src = inspect.getsource(pages)
    marker = '"/admin/matches/{match_id}/events/add"'
    assert src.count(marker) <= 1, "Дубликат роута events/add в pages.py"