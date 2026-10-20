import pytest
from httpx import ASGITransport, AsyncClient

from panel.app import Settings, create_app
from panel.manager import ServerManager
from panel.modules import ModuleInfo
from panel.modules.theship import TheShip

PW = "correct horse"
HEADER = "// BanList\r\n// steamid\tduration\tend_time\r\n"
ONE = HEADER + "// Some Player\r\n76561198033914776 60 2026-10-20T01:43:47:736\r\n"


@pytest.fixture
async def ship(tmp_path):
    mgr = ServerManager(tmp_path, modules={"theship": ModuleInfo("theship", "The Ship", "x", (), lambda c, s: TheShip(c, s))})
    app = create_app(Settings(PW, tmp_path), mgr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/api/login", json={"password": PW})
        sv = (await c.post("/api/servers", json={"module": "theship", "name": "Ship"})).json()
        c.sid, c.srv = sv["id"], mgr.get(sv["id"])
        c.file = c.srv.module._bans_path()
        yield c


async def test_reads_the_ban_file_with_name_and_end_time(ship):
    ship.file.parent.mkdir(parents=True, exist_ok=True)
    ship.file.write_bytes(ONE.encode())
    r = await ship.get(f"/api/servers/{ship.sid}/players")
    assert r.json()["lists"]["bans"] == [{"steamid": "76561198033914776", "name": "Some Player", "minutes": 60, "ends": "2026-10-20T01:43:47.736Z"}]
    assert r.json()["info"]["views"] == [["bans", "Banned"]]
    d = (await ship.get(f"/api/servers/{ship.sid}")).json()
    assert d["players"] is True and d["console_input"] is False


async def test_missing_file_is_an_empty_list(ship):
    assert (await ship.get(f"/api/servers/{ship.sid}/players")).json()["lists"]["bans"] == []


async def test_stopped_server_edits_the_file(ship):
    base = f"/api/servers/{ship.sid}/players/bans"
    r = await ship.post(f"{base}/add", json={"value": "76561198000000001", "minutes": 30})
    assert r.json()["applied"] == "file" and r.json()["lists"]["bans"][0]["minutes"] == 30
    assert ship.file.read_bytes().startswith(HEADER.encode()) and b"76561198000000001 30 " in ship.file.read_bytes()
    r = await ship.post(f"{base}/add", json={"value": "76561198000000002", "minutes": 0})
    assert [b["ends"] for b in r.json()["lists"]["bans"]][1] is None  # permanent
    r = await ship.post(f"{base}/remove", json={"value": "76561198000000001"})
    assert [b["steamid"] for b in r.json()["lists"]["bans"]] == ["76561198000000002"]
    r = await ship.post(f"{base}/clear", json={})
    assert r.json()["lists"]["bans"] == [] and ship.file.read_bytes() == HEADER.encode()


async def test_removing_keeps_other_bans_and_their_names(ship):
    ship.file.parent.mkdir(parents=True, exist_ok=True)
    ship.file.write_bytes((ONE + "// Other\r\n76561198000000003 15 2026-10-20T02:00:00:000\r\n").encode())
    r = await ship.post(f"/api/servers/{ship.sid}/players/bans/remove", json={"value": "76561198033914776"})
    assert r.json()["lists"]["bans"] == [{"steamid": "76561198000000003", "name": "Other", "minutes": 15, "ends": "2026-10-20T02:00:00.000Z"}]


async def test_running_server_still_edits_the_file_and_says_a_restart_is_needed(ship, monkeypatch):
    sent = []

    async def send(cmd):
        sent.append(cmd)
    sup = ship.srv.supervisor
    monkeypatch.setattr(sup, "send", send)
    monkeypatch.setattr(sup, "state", "running")
    base = f"/api/servers/{ship.sid}/players/bans"
    r = await ship.post(f"{base}/add", json={"value": "76561198000000001", "minutes": 45})
    assert r.json()["applied"] == "file" and r.json()["running"] is True and r.json()["live"] is False
    assert r.json()["info"]["restart_applies"] is True and r.json()["lists"]["bans"][0]["steamid"] == "76561198000000001"
    await ship.post(f"{base}/remove", json={"value": "76561198000000001"})
    r = await ship.post(f"{base}/clear", json={})
    assert r.json()["lists"]["bans"] == [] and sent == []  # nothing is typed to the game


async def test_bad_input_is_refused_before_anything_is_typed(ship, monkeypatch):
    sent = []

    async def send(cmd):
        sent.append(cmd)
    monkeypatch.setattr(ship.srv.supervisor, "send", send)
    monkeypatch.setattr(ship.srv.supervisor, "state", "running")
    base = f"/api/servers/{ship.sid}/players/bans"
    for body in ({"value": "123"}, {"value": "76561198000000001; quit"}, {"value": "76561198000000001", "minutes": "x"},
                 {"value": "76561198000000001", "minutes": -1}, {"value": "76561198000000001", "minutes": 10**9}):
        assert (await ship.post(f"{base}/add", json=body)).status_code == 400
    assert (await ship.post(f"/api/servers/{ship.sid}/players/whitelist/add", json={"value": "x"})).status_code == 400
    assert sent == []


async def test_ship_never_gets_the_minecraft_list_command(ship, monkeypatch):
    sent = []

    async def send(cmd):
        sent.append(cmd)
    monkeypatch.setattr(ship.srv.supervisor, "send", send)
    monkeypatch.setattr(ship.srv.supervisor, "state", "running")
    r = await ship.get(f"/api/servers/{ship.sid}/players/suggest")
    assert r.json() == {"online": [], "known": [], "live": False} and sent == []
