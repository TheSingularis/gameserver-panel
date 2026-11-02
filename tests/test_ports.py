import pytest
from httpx import ASGITransport, AsyncClient

from panel import netcheck
from panel.app import Settings, create_app
from panel.manager import ServerManager
from panel.modules import ModuleInfo
from panel.modules.base import Port
from panel.modules.minecraft import Minecraft
from panel.modules.theship import TheShip

PW = "correct horse"


@pytest.fixture
def no_listeners(monkeypatch):
    monkeypatch.setattr(netcheck, "listening", lambda proc=netcheck.PROC: {"tcp": set(), "udp": set()})


@pytest.fixture
async def api(tmp_path, no_listeners):
    mods = {"minecraft": ModuleInfo("minecraft", "Minecraft", "mc", (), lambda c, s: Minecraft(c, s)),
            "theship": ModuleInfo("theship", "The Ship", "x", (), lambda c, s: TheShip(c, s))}
    mgr = ServerManager(tmp_path, modules=mods)
    app = create_app(Settings(PW, tmp_path), mgr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/api/login", json={"password": PW})
        c.mgr = mgr

        async def add(kind="minecraft", name="Mine"):
            return (await c.post("/api/servers", json={"module": kind, "name": name, "options": {"flavor": "paper"} if kind == "minecraft" else {}})).json()["id"]
        c.add = add
        yield c


async def test_new_minecraft_servers_get_the_next_free_port(api):
    a, b, c = await api.add(name="A"), await api.add(name="B"), await api.add(name="C")
    assert [api.mgr.get(x).module.game_port() for x in (a, b, c)] == [25565, 25566, 25567]
    assert (await api.get(f"/api/servers/{b}/ports")).json()["ports"][0]["port"] == 25566  # the table follows the setting
    assert "server-port=25566" in (api.mgr.get(b).module.config_dir / "server.properties").read_text()


async def test_port_field_sets_the_port_and_refuses_one_another_server_has(api):
    a, b = await api.add(name="Survival"), await api.add(name="Creative")
    r = await api.put(f"/api/servers/{b}/port", json={"port": 25600})
    assert r.status_code == 200 and r.json()["game_port"] == 25600 and r.json()["conflict"] is None
    r = await api.put(f"/api/servers/{b}/port", json={"port": 25565})
    assert r.status_code == 409 and "Survival" in r.json()["detail"]
    assert (await api.get(f"/api/servers/{b}/ports")).json()["game_port"] == 25600  # unchanged by the refusal
    for bad in (80, 70000, "25565", True, None):
        assert (await api.put(f"/api/servers/{b}/port", json={"port": bad})).status_code == 400
    ship = await api.add("theship", "Ship")
    assert (await api.put(f"/api/servers/{ship}/port", json={"port": 7000})).status_code == 404  # fixed ports


async def test_config_tab_cannot_move_a_server_onto_a_taken_port(api):
    a, b = await api.add(name="Survival"), await api.add(name="Creative")
    cfg = f"/api/servers/{b}/config/server.properties"
    text = (await api.get(cfg)).json()["content"]
    r = await api.put(cfg, json={"content": text.replace("server-port=25566", "server-port=25565")})
    assert r.status_code == 409 and "Survival" in r.json()["detail"]
    assert "server-port=25566" in (await api.get(cfg)).json()["content"]  # file left as it was
    assert (await api.put(cfg, json={"content": text.replace("server-port=25566", "server-port=25999")})).status_code == 200


async def test_conflict_is_reported_when_two_servers_share_a_port(api):
    a, b = await api.add(name="A"), await api.add(name="B")
    (api.mgr.get(b).module.config_dir / "server.properties").write_text("server-port=25565\n")  # edited by hand
    r = (await api.get(f"/api/servers/{b}/ports")).json()
    assert "already used by A" in r["conflict"]


async def test_starting_is_blocked_while_another_server_runs_on_the_port(api, monkeypatch):
    a, b = await api.add(name="A"), await api.add(name="B")
    (api.mgr.get(b).module.config_dir / "server.properties").write_text("server-port=25565\n")
    monkeypatch.setattr(api.mgr.get(a).supervisor, "state", "running")
    r = await api.post(f"/api/servers/{b}/start")
    assert r.status_code == 409 and "A, which is running" in r.json()["detail"]


async def test_starting_is_blocked_by_something_else_on_the_port(api, monkeypatch):
    a = await api.add(name="A")
    monkeypatch.setattr(netcheck, "listening", lambda proc=netcheck.PROC: {"tcp": {25565}, "udp": set()})
    assert "something other than this server" in api.mgr.start_blocker(a)


def test_a_port_held_while_this_server_is_stopped_is_taken_not_listening(tmp_path):
    (tmp_path / "tcp").write_text("  sl  local_address rem_address   st\n   0: 00000000:63DD 00000000:0000 0A\n")  # 25565
    (tmp_path / "udp").write_text("  sl  local_address rem_address   st\n")
    row = netcheck.port_status([Port(25565, 25565, "tcp")], tmp_path, mine_running=False)[0]
    assert row["listening"] is False and row["taken"] is True
    row = netcheck.port_status([Port(25565, 25565, "tcp")], tmp_path, mine_running=True)[0]
    assert row["listening"] is True and row["taken"] is False


async def test_autostart_skips_a_server_whose_port_is_held_and_says_why(api, monkeypatch):
    a = api.mgr.get(await api.add(name="A"))
    monkeypatch.setattr(a.module, "is_installed", lambda: True)
    api.mgr.set_autostart(a.id, True)
    monkeypatch.setattr(netcheck, "listening", lambda proc=netcheck.PROC: {"tcp": {25565}, "udp": set()})

    async def no_sleep(_):
        pass
    await api.mgr.run_autostart(0, 0, sleep=no_sleep)
    assert any("auto-start skipped: Port 25565" in line for line in a.supervisor.tail(50))
