import pytest
from httpx import AsyncClient

@pytest.mark.asyncio
async def test_home_page(client: AsyncClient):
    response = await client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "Ближайшие матчи" in response.text

@pytest.mark.asyncio
async def test_tournaments_page(client: AsyncClient):
    response = await client.get("/tournaments")
    assert response.status_code == 200
    assert "Турниры" in response.text

@pytest.mark.asyncio
async def test_teams_page(client: AsyncClient):
    response = await client.get("/participants/teams")
    assert response.status_code == 200
    assert "Команды" in response.text

@pytest.mark.asyncio
async def test_nonexistent_tournament(client: AsyncClient):
    response = await client.get("/tournaments/999999")
    assert response.status_code == 404