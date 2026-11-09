import pytest
from sqlalchemy import select
from app.models.enums import MatchEventType, MatchStatus
from app.models.match_event import MatchEvent
from app.models.tournament import Tournament
from app.models.team import Team
from app.models.tournament_team import TournamentTeam

@pytest.mark.asyncio
async def test_admin_create_tournament_without_logo(admin_client, db):
    resp = await admin_client.post("/admin/tournaments/create", data={
        "name": "No Logo Cup", "division": "A1", "season": "2025",
        "age_category": "ADULTS", "status": "draft", "max_teams": "8"
    }, follow_redirects=False)
    assert resp.status_code == 303
    t = await db.scalar(select(Tournament).where(Tournament.name == "No Logo Cup"))
    assert t is not None and t.logo_url is None

@pytest.mark.asyncio
async def test_add_goal_with_assist(admin_client, db, seed_basic):
    m, teams, players = seed_basic["match"], seed_basic["teams"], seed_basic["players"]
    resp = await admin_client.post(f"/admin/matches/{m.id}/events/add", data={
        "team_id": teams[0].id, "player_id": players[0].id,
        "event_type": "goal", "minute": "15", "assist_player_id": players[1].id
    }, follow_redirects=False)
    assert resp.status_code == 303
    
    events = (await db.execute(select(MatchEvent).where(MatchEvent.match_id == m.id))).scalars().all()
    assert len(events) == 2
    assert any(e.event_type == MatchEventType.GOAL for e in events)
    assert any(e.event_type == MatchEventType.ASSIST for e in events)

@pytest.mark.asyncio
async def test_add_own_goal_changes_score(admin_client, db, seed_basic):
    m, teams, players = seed_basic["match"], seed_basic["teams"], seed_basic["players"]
    # Игрок команды А (home) забивает в свои ворота
    resp = await admin_client.post(f"/admin/matches/{m.id}/events/add", data={
        "team_id": teams[0].id, "player_id": players[0].id,
        "event_type": "own_goal", "minute": "20"
    }, follow_redirects=False)
    assert resp.status_code == 303
    
    await db.refresh(m)
    # Счет должен быть 0:1 в пользу гостей (Team B)
    assert m.home_score == 0
    assert m.away_score == 1

@pytest.mark.asyncio
async def test_admin_edit_event(admin_client, db, seed_basic):
    m, teams, players = seed_basic["match"], seed_basic["teams"], seed_basic["players"]
    # Создаем событие
    ev = MatchEvent(match_id=m.id, team_id=teams[0].id, player_id=players[0].id, event_type=MatchEventType.GOAL, minute=10)
    db.add(ev)
    await db.commit()
    
    # Редактируем (меняем минуту и игрока)
    resp = await admin_client.post(f"/admin/matches/{m.id}/events/{ev.id}/edit", data={
        "team_id": teams[1].id, "player_id": players[2].id,
        "event_type": "own_goal", "minute": "45"
    }, follow_redirects=False)
    assert resp.status_code == 303
    
    await db.refresh(ev)
    assert ev.event_type == MatchEventType.OWN_GOAL
    assert ev.minute == 45