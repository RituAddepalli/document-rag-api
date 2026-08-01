import pytest

from app.tests.conftest import requires_pg

pytestmark = [pytest.mark.asyncio, requires_pg]


async def test_signup_creates_user(client):
    resp = await client.post(
        "/auth/signup",
        json={"email": "alice@example.com", "password": "supersecret1", "full_name": "Alice"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == "alice@example.com"
    assert "hashed_password" not in body


async def test_signup_duplicate_email_rejected(client):
    payload = {"email": "bob@example.com", "password": "supersecret1"}
    first = await client.post("/auth/signup", json=payload)
    assert first.status_code == 201
    second = await client.post("/auth/signup", json=payload)
    assert second.status_code == 409


async def test_login_returns_jwt_and_me_works(client):
    await client.post(
        "/auth/signup", json={"email": "carol@example.com", "password": "supersecret1"}
    )
    login = await client.post(
        "/auth/login",
        data={"username": "carol@example.com", "password": "supersecret1"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "carol@example.com"


async def test_login_wrong_password_rejected(client):
    await client.post(
        "/auth/signup", json={"email": "dave@example.com", "password": "supersecret1"}
    )
    login = await client.post(
        "/auth/login",
        data={"username": "dave@example.com", "password": "wrong-password"},
    )
    assert login.status_code == 401


async def test_protected_route_requires_token(client):
    resp = await client.get("/auth/me")
    assert resp.status_code == 401
