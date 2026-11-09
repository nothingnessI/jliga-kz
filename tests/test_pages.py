import pytest

PUBLIC_PAGES = [
    "/", "/matches", "/tournaments", "/participants/teams", "/participants/players",
    "/news", "/media", "/stadiums", "/login", "/rating", "/disciplinary"
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", PUBLIC_PAGES)
async def test_public_pages_render(client, path):
    resp = await client.get(path)
    assert resp.status_code == 200, f"{path} вернул {resp.status_code}"


@pytest.mark.asyncio
async def test_match_detail_and_print_render(client, seed_basic):
    m = seed_basic["match"]
    resp1 = await client.get(f"/matches/{m.id}?tab=events")
    assert resp1.status_code == 200
    resp2 = await client.get(f"/matches/{m.id}?tab=protocol")
    assert resp2.status_code == 200

    resp3 = await client.get(f"/matches/{m.id}/print")
    assert resp3.status_code == 200
    # Проверяем именно с двоеточием, чтобы не ловить HTML-комментарий
    assert "АВТОГОЛЫ:" not in resp3.text


@pytest.mark.asyncio
async def test_team_application_render(client, seed_basic):
    t, team = seed_basic["tournament"], seed_basic["teams"][0]
    resp = await client.get(f"/tournaments/{t.id}/teams/{team.id}/application")
    assert resp.status_code == 200
    assert "ЗАЯВКА КОМАНДЫ" in resp.text