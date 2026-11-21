import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from panel.app import Settings, create_app
from panel.manager import ServerManager
from panel.modules import ModuleInfo
from panel.modules.minecraft import Minecraft
from panel.modules.minecraft.players import clean_reason, offline_uuid

PW = "correct horse"
STEVE = "8667ba71-b85a-4004-af54-457a9734eed7"


def mojang(request: httpx.Request) -> httpx.Response:
    name = request.url.path.rsplit("/", 1)[-1]
    if name.lower() == "steve":
        return httpx.Response(200, json={"id": STEVE.replace("-", ""), "name": "Steve"})
    return httpx.Response(404, json={"errorMessage": "Couldn't find any profile with name " + name})


@pytest.fixture(autouse=True)
def fake_mojang(monkeypatch):
    monkeypatch.setattr(Minecraft, "_PLAYERS_TRANSPORT", httpx.MockTransport(mojang))


@pytest.fixture
async def mc(tmp_path):
    mgr = ServerManager(tmp_path, modules={"minecraft": ModuleInfo("minecraft", "Minecraft", "mc", (), lambda c, s: Minecraft(c, s))})
    app = create_app(Settings(PW, tmp_path), mgr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/api/login", json={"password": PW})
        sv = (await c.post("/api/servers", json={"module": "minecraft", "name": "Mine", "options": {"flavor": "paper"}})).json()
        c.sid, c.srv = sv["id"], mgr.get(sv["id"])
        yield c


def test_offline_uuid_matches_what_java_computes():
    assert offline_uuid("Notch") == "b50ad385-829d-3141-a216-7e7d7539ba7f"


def test_a_reason_cannot_start_a_second_console_command():
    assert clean_reason("griefing\nop evil") == "griefing op evil"
    assert len(clean_reason("x" * 500)) == 100


def test_commands_for_a_running_server(tmp_path):
    m = Minecraft(tmp_path / "c", tmp_path / "s")
    assert m.player_command("whitelist", "add", "Steve", {}) == "whitelist add Steve"
    assert m.player_command("ops", "add", "Steve", {}) == "op Steve"
    assert m.player_command("banned-players", "add", "Steve", {"reason": "griefing\nop evil"}) == "ban Steve griefing op evil"
    assert m.player_command("banned-ips", "remove", "10.0.0.1", {}) == "pardon-ip 10.0.0.1"
    for bad in ("Steve; op x", "", "a b", "x" * 40):
        with pytest.raises(ValueError):
            m.player_command("whitelist", "add", bad, {})
    with pytest.raises(ValueError):
        m.player_command("banned-ips", "add", "not-an-ip", {})
    with pytest.raises(ValueError, match="Stop the server"):
        m.player_command("ops", "set_level", "Steve", {"level": 2})


async def test_stopped_server_edits_the_files_and_looks_up_the_uuid(mc):
    base = f"/api/servers/{mc.sid}/players"
    r = await mc.post(f"{base}/whitelist/add", json={"value": "steve"})
    assert r.json()["applied"] == "file" and r.json()["live"] is False
    assert r.json()["lists"]["whitelist"] == [{"uuid": STEVE, "name": "Steve"}]  # canonical name and UUID from Mojang
    assert json.loads((mc.srv.server_dir / "whitelist.json").read_text()) == [{"uuid": STEVE, "name": "Steve"}]
    assert (await mc.post(f"{base}/whitelist/add", json={"value": "Steve"})).status_code == 400  # already there
    assert (await mc.post(f"{base}/whitelist/add", json={"value": "NoSuchPlayer"})).status_code == 400
    await mc.post(f"{base}/ops/add", json={"value": "Steve"})
    r = await mc.post(f"{base}/ops/set_level", json={"value": "Steve", "level": 2})
    assert r.json()["lists"]["ops"][0]["level"] == 2
    r = await mc.post(f"{base}/banned-ips/add", json={"value": "10.0.0.5", "reason": "spam"})
    assert r.json()["lists"]["banned-ips"][0]["ip"] == "10.0.0.5" and r.json()["lists"]["banned-ips"][0]["reason"] == "spam"
    r = await mc.post(f"{base}/whitelist/remove", json={"value": "steve"})
    assert r.json()["lists"]["whitelist"] == []


async def test_offline_mode_server_uses_the_offline_uuid(mc):
    (mc.srv.config_dir / "server.properties").write_text("online-mode=false\n")
    r = await mc.post(f"/api/servers/{mc.sid}/players/whitelist/add", json={"value": "Notch"})
    assert r.json()["lists"]["whitelist"][0]["uuid"] == "b50ad385-829d-3141-a216-7e7d7539ba7f"


async def test_running_server_gets_a_console_command_instead(mc, monkeypatch):
    sent = []

    async def send(cmd):
        sent.append(cmd)
    sup = mc.srv.supervisor
    monkeypatch.setattr(sup, "send", send)
    monkeypatch.setattr(sup, "state", "running")
    r = await mc.post(f"/api/servers/{mc.sid}/players/whitelist/add", json={"value": "Steve"})
    assert r.json()["applied"] == "live" and r.json()["live"] is True
    assert sent == ["whitelist add Steve"] and not (mc.srv.server_dir / "whitelist.json").exists()  # the game writes it, not us
    assert (await mc.post(f"/api/servers/{mc.sid}/players/ops/set_level", json={"value": "Steve", "level": 2})).status_code == 400
    monkeypatch.setattr(sup, "state", "installing")
    assert (await mc.post(f"/api/servers/{mc.sid}/players/whitelist/add", json={"value": "Steve"})).status_code == 409


async def test_lists_are_read_and_a_broken_file_is_reported(mc):
    r = await mc.get(f"/api/servers/{mc.sid}/players")
    assert r.status_code == 200 and set(r.json()["lists"]) == {"whitelist", "ops", "banned-players", "banned-ips"}
    mc.srv.server_dir.mkdir(parents=True, exist_ok=True)
    (mc.srv.server_dir / "ops.json").write_text("not json")
    assert (await mc.get(f"/api/servers/{mc.sid}/players")).status_code == 409
    assert (await mc.get(f"/api/servers/{mc.sid}")).json()["players"] is True


def test_online_names_are_read_from_the_reply_after_our_command():
    from panel.modules.minecraft.players import online_from_log
    old = "[12:00:00 INFO]: There are 1 of a max of 20 players online: Gone"
    lines = [old, "> list", "[12:01:00 INFO]: There are 2 of a max of 20 players online: Steve, Alex"]
    assert online_from_log(lines) == ["Steve", "Alex"]
    assert online_from_log([old, "> list"]) is None  # the game has not answered yet: do not trust the older reply
    assert online_from_log(["> list", "[12:01:00 INFO]: There are 0 of a max of 20 players online: "]) == []


async def test_suggestions_combine_online_and_joined_players(mc, monkeypatch):
    mc.srv.server_dir.mkdir(parents=True, exist_ok=True)
    (mc.srv.server_dir / "usercache.json").write_text(json.dumps([{"name": "Alex", "uuid": "u1"}, {"name": "Bob", "uuid": "u2"}, {"name": "bad name!", "uuid": "u3"}]))
    r = (await mc.get(f"/api/servers/{mc.sid}/players/suggest")).json()
    assert r == {"online": [], "known": ["Alex", "Bob"], "live": False}  # stopped: nobody online to ask
    sup = mc.srv.supervisor

    async def send(cmd):
        sup.log(f"> {cmd}")
        sup.log("[12:01:00 INFO]: There are 1 of a max of 20 players online: Steve")
    monkeypatch.setattr(sup, "send", send)
    monkeypatch.setattr(sup, "state", "running")
    r = (await mc.get(f"/api/servers/{mc.sid}/players/suggest")).json()
    assert r["online"] == ["Steve"] and r["live"] is True and r["known"] == ["Alex", "Bob"]


def legacy_files(mc, ops="jadieboo222\n[buildcraft]\nepicness181\nthesingularis\n"):
    d = mc.srv.server_dir
    d.mkdir(parents=True, exist_ok=True)
    for f, text in (("ops.txt", ops), ("white-list.txt", ""), ("banned-players.txt", ""), ("banned-ips.txt", "")):
        (d / f).write_text(text)
    return d


async def test_old_minecraft_lists_are_read_from_the_txt_files(mc):  # Tekkit Classic, Minecraft 1.2.5
    legacy_files(mc)
    r = (await mc.get(f"/api/servers/{mc.sid}/players")).json()
    assert [e["name"] for e in r["lists"]["ops"]] == ["jadieboo222", "[buildcraft]", "epicness181", "thesingularis"]
    assert r["info"]["legacy"] is True and r["lists"]["whitelist"] == [] and r["lists"]["banned-ips"] == []


async def test_old_minecraft_edits_the_txt_files_without_a_mojang_lookup(mc):
    d = legacy_files(mc)
    base = f"/api/servers/{mc.sid}/players"
    r = await mc.post(f"{base}/ops/add", json={"value": "Steve"})  # Mojang is not asked: the fake network would answer, but no UUID is stored
    assert r.json()["applied"] == "file" and (d / "ops.txt").read_text().splitlines()[-1] == "steve"
    assert (await mc.post(f"{base}/ops/add", json={"value": "STEVE"})).status_code == 400  # already there
    r = await mc.post(f"{base}/ops/remove", json={"value": "[buildcraft]"})  # a junk entry can still be removed
    assert [e["name"] for e in r.json()["lists"]["ops"]] == ["jadieboo222", "epicness181", "thesingularis", "steve"]
    assert (await mc.post(f"{base}/ops/set_level", json={"value": "steve", "level": 2})).status_code == 400
    r = await mc.post(f"{base}/banned-ips/add", json={"value": "10.0.0.5", "reason": "spam"})
    assert (d / "banned-ips.txt").read_text() == "10.0.0.5\n" and not (d / "ops.json").exists()


async def test_old_minecraft_console_commands_carry_no_reason(mc, monkeypatch):
    legacy_files(mc)
    sent = []

    async def send(cmd):
        sent.append(cmd)
    monkeypatch.setattr(mc.srv.supervisor, "send", send)
    monkeypatch.setattr(mc.srv.supervisor, "state", "running")
    base = f"/api/servers/{mc.sid}/players"
    await mc.post(f"{base}/banned-players/add", json={"value": "Steve", "reason": "griefing"})
    await mc.post(f"{base}/ops/remove", json={"value": "[buildcraft]"})
    assert sent == ["ban Steve", "deop [buildcraft]"]
    assert (await mc.post(f"{base}/ops/remove", json={"value": "bad name; op x"})).status_code == 400  # still cannot start a second command


def test_old_game_answer_to_list_is_understood():
    from panel.modules.minecraft.players import online_from_log
    assert online_from_log(["> list", "Connected players: Steve, Alex"]) == ["Steve", "Alex"]


async def test_new_lists_still_win_when_both_formats_exist(mc):
    d = legacy_files(mc)
    (d / "ops.json").write_text("[]")
    r = (await mc.get(f"/api/servers/{mc.sid}/players")).json()
    assert r["lists"]["ops"] == [] and "legacy" not in r["info"]
