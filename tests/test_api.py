import pytest
from httpx import ASGITransport, AsyncClient

from panel.app import Settings, create_app
from tests.conftest import FakeModule

PW = "correct horse"


@pytest.fixture
async def client(tmp_path):
    m = FakeModule(tmp_path / "config", tmp_path / "server")
    app = create_app(Settings(PW, tmp_path), m)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
        await app.state.supervisor.stop()


async def login(c):
    r = await c.post("/api/login", json={"password": PW})
    assert r.status_code == 200


async def test_requires_login(client):
    for path in ("/api/status", "/api/module", "/api/logs", "/api/config/fake.cfg"):
        assert (await client.get(path)).status_code == 401
    assert (await client.post("/api/start")).status_code == 401


async def test_wrong_password_and_lockout(client):
    for _ in range(5):
        assert (await client.post("/api/login", json={"password": "nope"})).status_code == 401
    assert (await client.post("/api/login", json={"password": PW})).status_code == 429


async def test_lifecycle(client):
    await login(client)
    assert (await client.post("/api/start")).status_code == 409  # not installed
    assert (await client.post("/api/update")).status_code == 200
    assert (await client.post("/api/start")).json()["state"] == "running"
    assert (await client.post("/api/start")).status_code == 409
    assert (await client.post("/api/stop")).json()["state"] == "stopped"
    assert "installing" in (await client.get("/api/logs")).json()["lines"]


async def test_config_allowlist_and_roundtrip(client):
    await login(client)
    assert (await client.get("/api/config/../../etc/passwd")).status_code == 404
    assert (await client.get("/api/config/other.cfg")).status_code == 404
    assert (await client.put("/api/config/other.cfg", json={"content": "x"})).status_code == 404
    await client.put("/api/config/fake.cfg", json={"content": "name=hi"})
    assert (await client.get("/api/config/fake.cfg")).json()["content"] == "name=hi"


async def test_module_actions(client):
    await login(client)
    assert (await client.post("/api/actions/echo", json={"a": 1})).json() == {"echo": {"a": 1}}
    assert (await client.post("/api/actions/echo", json={"bad": 1})).status_code == 400
    assert (await client.post("/api/actions/nope", json={})).status_code == 404


async def test_tampered_cookie_rejected(client):
    client.cookies.set("gsp_session", "9999999999.deadbeef")
    assert (await client.get("/api/status")).status_code == 401
