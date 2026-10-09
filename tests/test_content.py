import gzip
import io
import json
import struct
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

from panel.app import Settings, create_app
from panel.manager import ServerManager
from panel.modules import ModuleInfo
from panel.modules.minecraft import Minecraft
from panel.modules.minecraft.content import read_datapack_state

PW = "correct horse"


def make_zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, c in files.items():
            z.writestr(n, c)
    return buf.getvalue()


PACK = make_zip({"pack.mcmeta": json.dumps({"pack": {"pack_format": 48, "description": "Cool pack"}}), "data/x/recipe/a.json": "{}"})
JAR = make_zip({"plugin.yml": "name: Essentials\nversion: '2.21'\nauthor: Bob\nmain: a.B\n"})


def level_dat(enabled, disabled) -> bytes:
    def s(t):
        b = t.encode()
        return struct.pack(">H", len(b)) + b

    def lst(items):
        return b"\x08" + struct.pack(">i", len(items)) + b"".join(s(i) for i in items)
    packs = b"\x09" + s("Enabled") + lst(enabled) + b"\x09" + s("Disabled") + lst(disabled) + b"\x00"
    data = b"\x0a" + s("DataPacks") + packs + b"\x00"
    root = b"\x0a" + s("") + b"\x0a" + s("Data") + data + b"\x00"
    return gzip.compress(root)


def make(tmp_path, flavor):
    return ServerManager(tmp_path, modules={"minecraft": ModuleInfo("minecraft", "Minecraft", "mc", (), lambda c, s: Minecraft(c, s))}), flavor


@pytest.fixture(params=["paper"])
async def mc(tmp_path, request):
    mgr, flavor = make(tmp_path, request.param)
    app = create_app(Settings(PW, tmp_path), mgr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/api/login", json={"password": PW})
        sv = (await c.post("/api/servers", json={"module": "minecraft", "name": "Mine", "options": {"flavor": flavor}})).json()
        c.sid, c.srv = sv["id"], mgr.get(sv["id"])
        c.dir = c.srv.module.server_dir
        (c.dir / "world").mkdir(parents=True, exist_ok=True)
        yield c


async def up(c, kind, name, data):
    return await c.post(f"/api/servers/{c.sid}/content/{kind}/upload", params={"name": name}, content=data)


def test_level_dat_state_is_read(tmp_path):
    p = tmp_path / "level.dat"
    p.write_bytes(level_dat(["vanilla", "file/a.zip"], ["file/b.zip"]))
    assert read_datapack_state(p) == (["vanilla", "file/a.zip"], ["file/b.zip"])
    assert read_datapack_state(tmp_path / "missing.dat") is None
    p.write_bytes(b"junk")
    assert read_datapack_state(p) is None


async def test_tabs_follow_the_flavor(mc):
    d = (await mc.get(f"/api/servers/{mc.sid}")).json()
    assert [k["id"] for k in d["content"]] == ["datapacks", "plugins"]
    mc.srv.module.config_dir.joinpath("panel.properties").write_text("flavor=vanilla\n")
    d = (await mc.get(f"/api/servers/{mc.sid}")).json()
    assert [k["id"] for k in d["content"]] == ["datapacks"]
    assert (await mc.get(f"/api/servers/{mc.sid}/content/plugins")).status_code == 404


async def test_datapack_upload_stopped_and_toggle(mc):
    r = await up(mc, "datapacks", "Cool.zip", PACK)
    assert r.status_code == 200 and r.json()["applied"] == "file"
    item = r.json()["items"][0]
    assert item["name"] == "Cool.zip" and item["enabled"] and item["description"] == "Cool pack" and item["format"] == "48"
    assert (mc.dir / "world/datapacks/Cool.zip").is_file()
    r = await mc.post(f"/api/servers/{mc.sid}/content/datapacks/disable", json={"name": "Cool.zip"})
    assert not r.json()["items"][0]["enabled"] and r.json()["items"][0]["aside"]
    assert (mc.dir / "datapacks-disabled/world/Cool.zip").is_file() and not (mc.dir / "world/datapacks/Cool.zip").exists()
    r = await mc.post(f"/api/servers/{mc.sid}/content/datapacks/enable", json={"name": "Cool.zip"})
    assert r.json()["items"][0]["enabled"]
    assert (await mc.post(f"/api/servers/{mc.sid}/content/datapacks/remove", json={"name": "Cool.zip"})).json()["items"] == []


async def test_bad_datapacks_are_refused_with_a_reason(mc):
    nested = make_zip({"Folder/pack.mcmeta": "{}", "Folder/data/a": "x"})
    r = await up(mc, "datapacks", "n.zip", nested)
    assert r.status_code == 400 and "inside a folder" in r.json()["detail"]
    assert "no pack.mcmeta" in (await up(mc, "datapacks", "n.zip", make_zip({"a.txt": "x"}))).json()["detail"]
    assert (await up(mc, "datapacks", "n.zip", b"not a zip")).status_code == 400
    assert (await up(mc, "datapacks", "n.zip", make_zip({"../evil": "x", "pack.mcmeta": "{}"}))).status_code == 400
    assert (await up(mc, "datapacks", "bad name;rm.zip", PACK)).status_code == 400
    assert (await up(mc, "datapacks", "../../x.zip", PACK)).json()["items"][0]["name"] == "x.zip"  # path parts are dropped
    assert list((mc.dir / "world/datapacks").iterdir()) == [mc.dir / "world/datapacks/x.zip"]


async def test_running_server_gets_commands_and_keeps_files_alone(mc, monkeypatch):
    sent = []
    sup = mc.srv.supervisor

    async def send(cmd):
        sent.append(cmd)
    monkeypatch.setattr(sup, "send", send)
    monkeypatch.setattr(sup, "state", "running")
    r = await up(mc, "datapacks", "Cool.zip", PACK)
    assert r.json()["applied"] == "live" and sent == ["minecraft:reload"] and r.json()["items"][0]["enabled"]
    r = await mc.post(f"/api/servers/{mc.sid}/content/datapacks/disable", json={"name": "Cool.zip"})
    assert sent[-1] == 'datapack disable "file/Cool.zip"' and not r.json()["items"][0]["enabled"]
    assert (mc.dir / "world/datapacks/Cool.zip").is_file()  # a running server keeps its files
    r = await mc.post(f"/api/servers/{mc.sid}/content/datapacks/remove", json={"name": "Cool.zip"})
    assert r.status_code == 409
    r = await mc.post(f"/api/servers/{mc.sid}/content/datapacks/enable", json={"name": 'x"; op me'})
    assert r.status_code == 400
    assert (await up(mc, "plugins", "E.jar", JAR)).status_code == 409
    monkeypatch.setattr(sup, "state", "installing")
    assert (await mc.get(f"/api/servers/{mc.sid}/content/datapacks")).status_code == 200
    assert (await up(mc, "datapacks", "Cool.zip", PACK)).status_code == 409


async def test_a_pack_the_world_switched_off_cannot_be_enabled_from_a_file(mc):
    (mc.dir / "world/datapacks").mkdir(parents=True)
    (mc.dir / "world/datapacks/Off.zip").write_bytes(PACK)
    (mc.dir / "world/level.dat").write_bytes(level_dat(["vanilla"], ["file/Off.zip"]))
    item = (await mc.get(f"/api/servers/{mc.sid}/content/datapacks")).json()["items"][0]
    assert not item["enabled"] and item["needs_live"]
    r = await mc.post(f"/api/servers/{mc.sid}/content/datapacks/enable", json={"name": "Off.zip"})
    assert r.status_code == 409 and "Start the server" in r.json()["detail"]


async def test_plugins_keep_their_config_folder(mc):
    r = await up(mc, "plugins", "Essentials.jar", JAR)
    assert r.status_code == 200
    plug = mc.dir / "plugins"
    (plug / "Essentials").mkdir()
    (plug / "Essentials/config.yml").write_text("keep: me")
    item = (await mc.get(f"/api/servers/{mc.sid}/content/plugins")).json()["items"][0]
    assert item["name"] == "Essentials.jar" and item["version"] == "2.21" and item["authors"] == "Bob" and item["has_config"]
    await up(mc, "plugins", "Essentials.jar", JAR)  # replace
    r = await mc.post(f"/api/servers/{mc.sid}/content/plugins/disable", json={"name": "Essentials.jar"})
    assert (plug / "Essentials.jar.off").is_file() and not r.json()["items"][0]["enabled"]
    await mc.post(f"/api/servers/{mc.sid}/content/plugins/enable", json={"name": "Essentials.jar.off"})
    await mc.post(f"/api/servers/{mc.sid}/content/plugins/remove", json={"name": "Essentials.jar"})
    assert not (plug / "Essentials.jar").exists() and (plug / "Essentials/config.yml").read_text() == "keep: me"


async def test_plugin_uploads_must_look_like_plugins(mc):
    r = await up(mc, "plugins", "Mod.jar", make_zip({"META-INF/mods.toml": "x"}))
    assert r.status_code == 400 and "plugin.yml" in r.json()["detail"]
    assert (await up(mc, "plugins", "x.zip", JAR)).status_code == 400


from panel.modules.minecraft.content import declared_range, format_fits, server_data_format  # noqa: E402


def write_jar(d, pack_version):
    (d / "server.jar").write_bytes(make_zip({"version.json": json.dumps({"id": "x", "pack_version": pack_version})}))


def test_server_format_from_either_version_json_schema(tmp_path):
    assert server_data_format(tmp_path) is None
    write_jar(tmp_path, {"resource": 64, "data": 81})  # 1.21.8
    assert server_data_format(tmp_path) == (81, 0)
    write_jar(tmp_path, {"resource_major": 97, "resource_minor": 1, "data_major": 121, "data_minor": 2})  # 26.3 style
    assert server_data_format(tmp_path) == (121, 2)
    (tmp_path / "server.jar").write_bytes(b"junk")
    assert server_data_format(tmp_path) is None


@pytest.mark.parametrize("pack,server,fits", [
    ({"pack_format": 81}, (81, 0), True),
    ({"pack_format": 48}, (81, 0), False),
    ({"pack_format": 48, "supported_formats": [48, 81]}, (81, 0), True),
    ({"pack_format": 48, "supported_formats": {"min_inclusive": 48, "max_inclusive": 81}}, (60, 0), True),
    ({"min_format": 48, "max_format": 81}, (81, 3), True),  # a bare number covers every minor of that major
    ({"min_format": [82, 0], "max_format": [82, 1]}, (82, 2), False),
    ({"min_format": [82, 0], "max_format": [82, 1]}, (82, 1), True),
    ({"min_format": 90}, (81, 0), False),
    ({}, (81, 0), None),
])
def test_pack_range_against_the_server(pack, server, fits):
    assert format_fits(declared_range(pack), server) is fits


def test_unknown_server_format_means_no_verdict():
    assert format_fits(declared_range({"pack_format": 48}), None) is None


async def test_list_flags_packs_for_other_versions(mc):
    write_jar(mc.dir, {"resource": 64, "data": 81})
    await up(mc, "datapacks", "Old.zip", PACK)  # pack_format 48
    newer = make_zip({"pack.mcmeta": json.dumps({"pack": {"pack_format": 81, "description": "n"}}), "data/a": "x"})
    r = await up(mc, "datapacks", "New.zip", newer)
    got = {i["name"]: i["fits"] for i in r.json()["items"]}
    assert got == {"New.zip": True, "Old.zip": False}
    assert "range" not in r.json()["items"][0]
