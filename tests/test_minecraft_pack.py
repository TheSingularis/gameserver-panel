import io
import json
import zipfile

import pytest
from httpx import ASGITransport, AsyncClient

from panel.app import Settings, create_app
from panel.manager import ServerManager
from panel.modules import catalog
from panel.modules import minecraft as mc
from panel.modules.minecraft import Minecraft, detect_entry, java_major_for
from panel.supervisor import Supervisor

FORGE_ARGS = "libraries/net/minecraftforge/forge/1.20.1-47.2.0/unix_args.txt"


def build_zip(path, entries, wrapper="ATM-Server/"):
    with zipfile.ZipFile(path, "w") as z:
        for name, body in entries.items():
            i = zipfile.ZipInfo(wrapper + name)
            i.external_attr = (0o755 if name.endswith(".sh") else 0o644) << 16
            z.writestr(i, body)
    return path


PACK = {"run.sh": "#!/bin/bash\njava @user_jvm_args.txt @" + FORGE_ARGS + " nogui\n", "user_jvm_args.txt": "# memory\n-Xms512M\n-Xmx1G\n-XX:+UseG1GC\n",
        "mods/old.jar": "old", FORGE_ARGS: "-p libs", "server.properties": "motd=From the pack\nserver-port=25565\n",
        "eula.txt": "eula=true\n", "world/level.dat": "from-zip"}


def pack_server(tmp_path, java_homes=True, monkeypatch=None):
    m = Minecraft(tmp_path / "config", tmp_path / "server")
    m.prepare({"flavor": "pack"})
    if java_homes and monkeypatch:
        for v in ("8", "17", "21", "25"):
            (tmp_path / "jvm" / v / "bin").mkdir(parents=True)
            (tmp_path / "jvm" / v / "bin" / "java").write_text("")
        monkeypatch.setattr(mc, "JAVA_HOMES", tmp_path / "jvm")
    return m


def test_java_choice_follows_minecraft_version():
    got = {v: java_major_for(v) for v in ("1.12.2", "1.16.5", "1.17.1", "1.19.2", "1.20.4", "1.20.5", "1.21.8", "26.3")}
    assert got == {"1.12.2": 8, "1.16.5": 8, "1.17.1": 17, "1.19.2": 17, "1.20.4": 17, "1.20.5": 21, "1.21.8": 21, "26.3": 25}


def test_prompts_ask_for_the_zip_until_one_is_uploaded(tmp_path):
    m = pack_server(tmp_path, False)
    assert [p["id"] for p in m.prompts()] == ["pack", "eula"] and m.prompts()[0]["kind"] == "upload"
    assert not m.is_installed()
    with pytest.raises(RuntimeError, match="Upload your modpack"):
        import asyncio
        asyncio.run(m.install(lambda l: None))


def test_first_upload_unpacks_detects_and_adopts_settings_but_not_the_eula(tmp_path):
    m = pack_server(tmp_path, False)
    logs = []
    m.accept_upload(build_zip(tmp_path / "p.zip", PACK), logs.append)
    state = json.loads((m.server_dir / ".panel-pack.json").read_text())
    assert state == {"kind": "script", "entry": "run.sh", "mc": "1.20.1"}
    assert m.is_installed() and (m.server_dir / "mods" / "old.jar").exists() and (m.server_dir / "world" / "level.dat").read_text() == "from-zip"
    assert "From the pack" in (m.config_dir / "server.properties").read_text()  # its settings seed ours, once
    assert not (m.server_dir / "eula.txt").exists() and not m.eula_accepted()  # the pack author's acceptance is not yours
    assert not (m.server_dir.parent / "server.incoming").exists()


def test_reupload_replaces_mods_but_keeps_world_and_player_lists(tmp_path):
    m = pack_server(tmp_path, False)
    m.accept_upload(build_zip(tmp_path / "p1.zip", PACK), lambda l: None)
    (m.server_dir / "world" / "level.dat").write_text("my-progress")
    (m.server_dir / "ops.json").write_text("[admin]")
    (m.config_dir / "server.properties").write_text("motd=Edited by me\n")
    v2 = {k: v for k, v in PACK.items() if k != "mods/old.jar"} | {"mods/new.jar": "new"}
    m.accept_upload(build_zip(tmp_path / "p2.zip", v2), lambda l: None)
    assert not (m.server_dir / "mods" / "old.jar").exists() and (m.server_dir / "mods" / "new.jar").exists()
    assert (m.server_dir / "world" / "level.dat").read_text() == "my-progress"  # the pack's own world does not overwrite yours
    assert (m.server_dir / "ops.json").read_text() == "[admin]"
    assert (m.config_dir / "server.properties").read_text() == "motd=Edited by me\n"  # your edited settings win over the pack's


def test_bad_zip_changes_nothing(tmp_path):
    m = pack_server(tmp_path, False)
    m.accept_upload(build_zip(tmp_path / "p1.zip", PACK), lambda l: None)
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("../escape.txt", "x")
    with pytest.raises(ValueError, match="unsafe path"):
        m.accept_upload(evil, lambda l: None)
    assert (m.server_dir / "mods" / "old.jar").exists() and not (tmp_path / "escape.txt").exists()


def test_launch_runs_the_packs_script_with_the_java_its_version_needs(tmp_path, monkeypatch):
    m = pack_server(tmp_path, monkeypatch=monkeypatch)
    m.accept_upload(build_zip(tmp_path / "p.zip", PACK), lambda l: None)
    (m.config_dir / "panel.properties").write_text("flavor=pack\nmemory=6G\n")
    (m.config_dir / "eula.txt").write_text("eula=true\n")
    spec = m.launch_spec()
    assert spec.argv == ["bash", "run.sh"] and spec.cwd == m.server_dir
    assert spec.env["JAVA_HOME"] == str(tmp_path / "jvm" / "17") and spec.env["PATH"].startswith(str(tmp_path / "jvm" / "17" / "bin") + ":")  # 1.20.1 -> Java 17
    jvm = (m.server_dir / "user_jvm_args.txt").read_text().splitlines()
    assert "-Xmx6G" in jvm and "-Xmx1G" not in jvm and "-Xms512M" not in jvm and "-XX:+UseG1GC" in jvm  # memory set, other flags kept
    (m.config_dir / "panel.properties").write_text("flavor=pack\njava=25\n")
    assert m.launch_spec().env["JAVA_HOME"].endswith("/25")  # the setting wins over the guess


def test_args_and_jar_packs(tmp_path, monkeypatch):
    m = pack_server(tmp_path, monkeypatch=monkeypatch)
    m.accept_upload(build_zip(tmp_path / "a.zip", {k: v for k, v in PACK.items() if k != "run.sh"}), lambda l: None)
    (m.config_dir / "eula.txt").write_text("eula=true\n")
    argv = m.launch_spec().argv
    assert argv[1:] == ["@user_jvm_args.txt", "@" + FORGE_ARGS, "--nogui"]
    m2 = pack_server(tmp_path / "two", monkeypatch=monkeypatch)
    m2.accept_upload(build_zip(tmp_path / "j.zip", {"forge-1.12.2-14.23.5.jar": "j", "forge-1.12.2-installer.jar": "i", "mods/m.jar": "m"}), lambda l: None)
    (m2.config_dir / "eula.txt").write_text("eula=true\n")
    assert detect_entry(m2.server_dir) == {"kind": "jar", "entry": "forge-1.12.2-14.23.5.jar"}
    assert m2.launch_spec().argv[1:] == ["-Xmx2G", "-jar", "forge-1.12.2-14.23.5.jar", "--nogui"]


def test_unrecognised_pack_needs_a_start_file_and_it_cannot_escape(tmp_path, monkeypatch):
    m = pack_server(tmp_path, monkeypatch=monkeypatch)
    logs = []
    m.accept_upload(build_zip(tmp_path / "p.zip", {"weird/launcher.sh": "x", "readme.txt": "hi"}), logs.append)
    assert m.is_installed() and any("could not tell" in l for l in logs)
    (m.config_dir / "eula.txt").write_text("eula=true\n")
    with pytest.raises(RuntimeError, match="Start file"):
        m.launch_spec()
    (m.config_dir / "panel.properties").write_text("flavor=pack\nstart_file=weird/launcher.sh\n")
    assert m.launch_spec().argv == ["bash", "weird/launcher.sh"]
    (m.config_dir / "panel.properties").write_text("flavor=pack\nstart_file=../../etc/passwd\n")
    with pytest.raises(RuntimeError, match="not found inside"):
        m.launch_spec()


def test_pack_servers_refuse_clean_reinstall_and_other_flavors_refuse_uploads(tmp_path):
    m = pack_server(tmp_path, False)
    assert "upload its server zip again" in m.reinstall_blocker() and m.describe()["can_reinstall"] is False
    paper = Minecraft(tmp_path / "pc", tmp_path / "ps")
    paper.prepare({"flavor": "paper"})
    assert paper.reinstall_blocker() is None
    with pytest.raises(RuntimeError, match="Set Server type to pack"):
        paper.accept_upload(tmp_path / "x.zip", lambda l: None)


async def test_supervisor_upload_needs_a_stopped_server(tmp_path):
    m = pack_server(tmp_path, False)
    s = Supervisor(m)
    await s.upload(build_zip(tmp_path / "p.zip", PACK))
    assert s.state == "stopped" and m.is_installed()
    s.state = "running"
    with pytest.raises(RuntimeError, match="stop the server first"):
        await s.upload(tmp_path / "p.zip")
    s.state = "stopped"


@pytest.fixture
async def api(tmp_path):
    mgr = ServerManager(tmp_path, modules=catalog())
    app = create_app(Settings("correct horse", tmp_path), mgr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/api/login", json={"password": "correct horse"})).status_code == 200
        yield c, tmp_path


async def test_add_with_options_then_upload_through_the_api(api):
    c, tmp = api
    games = {g["id"]: g for g in (await c.get("/api/games")).json()["games"]}
    assert games["minecraft"]["options"][0]["choices"] == ["paper", "vanilla", "pack"]
    assert (await c.post("/api/servers", json={"module": "minecraft", "name": "Mods", "options": {"flavor": "nope"}})).status_code == 409
    assert (await c.post("/api/servers", json={"module": "minecraft", "name": "Mods", "options": {"flavor": "pack"}})).status_code == 200
    detail = (await c.get("/api/servers/mods")).json()
    assert detail["upload_accept"] == ".zip" and detail["prompts"][0]["kind"] == "upload"
    buf = io.BytesIO()
    build_zip(buf, PACK)
    r = await c.post("/api/servers/mods/upload", content=buf.getvalue())
    assert r.status_code == 200 and r.json()["installed"] is True
    assert not list((tmp / ".uploads").iterdir())  # the temp copy is cleaned up
    bad = await c.post("/api/servers/mods/upload", content=b"not a zip")
    assert bad.status_code == 400 and "not a valid zip" in bad.json()["detail"]
    assert (await c.post("/api/servers/mods/upload", content=b"")).status_code == 400


async def test_upload_is_login_protected_and_only_for_games_that_take_it(tmp_path):
    mgr = ServerManager(tmp_path, modules=catalog(demo=True))
    app = create_app(Settings("correct horse", tmp_path), mgr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        assert (await c.post("/api/servers/x/upload", content=b"z")).status_code == 401
        await c.post("/api/login", json={"password": "correct horse"})
        await c.post("/api/servers", json={"module": "demo"})
        assert (await c.post("/api/servers/demo-game/upload", content=b"z")).status_code == 404
