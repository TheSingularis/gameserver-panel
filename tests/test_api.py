import pytest
from httpx import ASGITransport, AsyncClient

from panel.app import Settings, create_app
from panel.manager import ServerManager
from tests.conftest import FAKE_CATALOG

PW = "correct horse"


@pytest.fixture
async def client(tmp_path):
    mgr = ServerManager(tmp_path, modules=FAKE_CATALOG)
    app = create_app(Settings(PW, tmp_path), mgr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c
        for s in list(mgr.servers.values()):
            await s.supervisor.stop()


@pytest.fixture
async def ready(client):
    """Logged in, with the fake game already added."""
    assert (await client.post("/api/login", json={"password": PW})).status_code == 200
    assert (await client.post("/api/servers", json={"module": "fake"})).status_code == 200
    return client


async def test_requires_login(client):
    for path in ("/api/servers", "/api/games", "/api/servers/fake/status", "/api/servers/fake/logs",
                 "/api/servers/fake/config/fake.cfg", "/api/servers/fake/ports"):
        assert (await client.get(path)).status_code == 401
    assert (await client.post("/api/servers/fake/start")).status_code == 401
    assert (await client.post("/api/servers", json={"module": "fake"})).status_code == 401


async def test_wrong_password_and_lockout(client):
    for _ in range(5):
        assert (await client.post("/api/login", json={"password": "nope"})).status_code == 401
    assert (await client.post("/api/login", json={"password": PW})).status_code == 429


async def test_catalogue_add_and_remove(client):
    await client.post("/api/login", json={"password": PW})
    games = {g["id"]: g for g in (await client.get("/api/games")).json()["games"]}
    assert games["fake"]["status"] == "available" and games["fake"]["servers"] == 0
    assert games["soon"]["status"] == "soon"
    assert (await client.get("/api/servers")).json()["servers"] == []
    assert (await client.post("/api/servers", json={"module": "soon"})).status_code == 409  # coming soon
    assert (await client.post("/api/servers", json={"module": "nope"})).status_code == 409
    assert (await client.post("/api/servers", json={"module": "fake"})).status_code == 200
    assert (await client.post("/api/servers", json={"module": "fake", "name": "Second one"})).status_code == 200  # several per game
    rows = (await client.get("/api/servers")).json()["servers"]
    assert [r["id"] for r in rows] == ["fake", "second-one"] and rows[1]["name"] == "Second one"
    assert rows[0]["state"] == "stopped" and rows[0]["game"] == "Fake"
    r = await client.patch("/api/servers/second-one", json={"name": "  Friday   night  "})
    assert r.json()["name"] == "Friday night" and r.json()["id"] == "second-one"
    assert (await client.delete("/api/servers/fake")).status_code == 200
    assert (await client.get("/api/servers/fake/status")).status_code == 404


async def test_lifecycle_and_progress_field(ready):
    c = ready
    base = "/api/servers/fake"
    st = (await c.get(base + "/status")).json()
    assert st["installed"] is False and st["progress"] is None
    assert (await c.post(base + "/start")).status_code == 409  # not installed
    assert (await c.post(base + "/update")).status_code == 200
    assert (await c.post(base + "/start")).json()["state"] == "running"
    assert (await c.post(base + "/start")).status_code == 409
    assert (await c.post(base + "/reinstall")).json()["state"] == "running"  # wipes, installs, restarts
    assert (await c.post(base + "/stop")).json()["state"] == "stopped"
    assert "installing" in (await c.get(base + "/logs")).json()["lines"]


async def test_config_allowlist_and_roundtrip(ready):
    c, base = ready, "/api/servers/fake/config/"
    assert (await c.get(base + "../../etc/passwd")).status_code == 404
    assert (await c.get(base + "other.cfg")).status_code == 404
    assert (await c.put(base + "other.cfg", json={"content": "x"})).status_code == 404
    await c.put(base + "fake.cfg", json={"content": "name=hi"})
    assert (await c.get(base + "fake.cfg")).json()["content"] == "name=hi"


async def test_module_actions(ready):
    c, base = ready, "/api/servers/fake/actions/"
    assert (await c.post(base + "echo", json={"a": 1})).json() == {"echo": {"a": 1}}
    assert (await c.post(base + "echo", json={"bad": 1})).status_code == 400
    assert (await c.post(base + "nope", json={})).status_code == 404


async def test_ports_and_detail(ready):
    rows = (await ready.get("/api/servers/fake/ports")).json()["ports"]
    assert [r["port"] for r in rows] == [1000, 1001] and rows[0]["proto"] == "udp"
    await ready.patch("/api/servers/fake", json={"name": "Mine"})
    d = (await ready.get("/api/servers/fake")).json()
    assert d["name"] == "Mine" and d["game"] == "Fake"
    assert d["config_files"] == ["fake.cfg"] and "echo" in d["actions"]


async def test_tampered_cookie_rejected(client):
    client.cookies.set("gsp_session", "9999999999.deadbeef")
    assert (await client.get("/api/servers")).status_code == 401


def test_config_schema_is_described(tmp_path):
    from panel.modules.demo import Demo
    d = Demo(tmp_path / "c", tmp_path / "s").describe()
    assert d["config_schema"]["demo.cfg"][0] == {"key": "name", "label": "Server name", "type": "text", "help": "Shown in the server list", "options": (), "choices_from": False, "older_warning": False, "applies_when": {}}


async def test_index_is_never_served_from_browser_cache(client):
    r = await client.get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


async def test_page_carries_its_build_and_api_reports_it(client, monkeypatch):
    monkeypatch.setenv("PANEL_COMMIT", "abcdef0123456789")
    monkeypatch.setenv("PANEL_BUILT", "2026-10-05")
    r = await client.get("/")
    assert 'name="panel-build" content="abcdef0|2026-10-05"' in r.text
    assert (await client.get("/api/version")).status_code == 401  # needs login like the rest of the API


async def test_version_endpoint_when_logged_in(ready, monkeypatch):
    monkeypatch.setenv("PANEL_COMMIT", "abcdef0123456789")
    assert (await ready.get("/api/version")).json() == {"commit": "abcdef0", "built": ""}


async def test_autostart_toggle_via_api(ready):
    assert (await ready.get("/api/servers/fake")).json()["autostart"] is False
    r = await ready.patch("/api/servers/fake", json={"autostart": True})
    assert r.json()["autostart"] is True
    d = (await ready.get("/api/servers/fake")).json()
    assert d["autostart"] is True and d["autostart_delay"] == 60 and d["autostart_stagger"] == 30
    assert (await ready.patch("/api/servers/fake", json={"autostart": "yes"})).status_code == 400
    assert (await ready.patch("/api/servers/fake", json={"autostart": False})).json()["name"] == "Fake"  # name untouched


async def test_command_endpoint(ready):
    c, base = ready, "/api/servers/fake"
    assert (await c.get(base)).json()["console_input"] is False
    assert (await c.post(base + "/command", json={"command": "list"})).status_code == 409


async def test_game_without_player_lists_has_no_players_endpoint(ready):
    assert (await ready.get("/api/servers/fake/players")).status_code == 404
    assert (await ready.get("/api/servers/fake")).json()["players"] is False
