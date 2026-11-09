import pytest

@pytest.mark.asyncio
async def test_api_login_and_me(client, admin_user):
    login = await client.post(
        "/api/v1/auth/login",
        data={"username": "admin@example.com", "password": "adminpass123"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "admin@example.com"
    assert me.json()["role"] == "admin"

@pytest.mark.asyncio
async def test_api_create_team_requires_admin(client, admin_user):
    resp = await client.post("/api/v1/teams/", json={"name": "XY", "slug": "xy"})
    assert resp.status_code == 401

    login = await client.post(
        "/api/v1/auth/login",
        data={"username": "admin@example.com", "password": "adminpass123"},
    )
    token = login.json()["access_token"]

    resp = await client.post(
        "/api/v1/teams/",
        json={"name": "XY", "slug": "xy"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201