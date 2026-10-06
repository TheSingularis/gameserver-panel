import hashlib
import json

import httpx
import pytest

from panel.modules.minecraft import MARKER, Minecraft

JAR_BYTES = b"PK-fake-server-jar" * 100


def fake_network(hits, bad_sum=False):
    sha1 = hashlib.sha1(JAR_BYTES).hexdigest()
    sha256 = hashlib.sha256(JAR_BYTES).hexdigest()

    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        hits.append(url)
        assert "gameserver-panel" in req.headers["user-agent"]
        if url.endswith("version_manifest_v2.json"):
            return httpx.Response(200, json={"latest": {"release": "1.21.8"}, "versions": [
                {"id": "1.21.8", "url": "https://piston.test/1.21.8.json"}, {"id": "1.20.4", "url": "https://piston.test/1.20.4.json"}]})
        if url.startswith("https://piston.test/"):
            return httpx.Response(200, json={"downloads": {"server": {"sha1": "0" * 40 if bad_sum else sha1, "url": "https://dl.test/vanilla.jar"}}})
        if url == "https://fill.papermc.io/v3/projects/paper":
            return httpx.Response(200, json={"versions": {"1.21": ["1.21.8", "1.21.7", "1.21.9-rc1"], "1.20": ["1.20.4"]}})
        if url.endswith("/versions/1.21.8/builds"):
            return httpx.Response(200, json=[
                {"id": 5, "channel": "ALPHA", "downloads": {"server:default": {"url": "https://dl.test/p5.jar", "checksums": {"sha256": "x"}}}}])
        if url.endswith("/versions/1.21.7/builds"):
            return httpx.Response(200, json=[
                {"id": 30, "channel": "STABLE", "downloads": {"server:default": {"url": "https://dl.test/p30.jar", "checksums": {"sha256": sha256}}}},
                {"id": 31, "channel": "STABLE", "downloads": {"server:default": {"url": "https://dl.test/p31.jar", "checksums": {"sha256": sha256}}}},
                {"id": 32, "channel": "BETA", "downloads": {"server:default": {"url": "https://dl.test/p32.jar", "checksums": {"sha256": "y"}}}}])
        if url.startswith("https://dl.test/"):
            return httpx.Response(200, content=JAR_BYTES, headers={"content-length": str(len(JAR_BYTES))})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def make(tmp_path, flavor="vanilla", hits=None, **kw):
    m = Minecraft(tmp_path / "config", tmp_path / "server")
    m.prepare()
    (m.config_dir / "panel.properties").write_text(f"flavor={flavor}\nversion=latest\nmemory=3G\n")
    m._transport = fake_network(hits if hits is not None else [], **kw)
    return m


def test_prepare_seeds_settings_and_eula_prompt_until_accepted(tmp_path):
    m = Minecraft(tmp_path / "c", tmp_path / "s")
    m.prepare()
    assert "server-port=25565" in (m.config_dir / "server.properties").read_text()
    assert m.describe()["prompts"][0]["action"] == "accept_eula"
    assert set(m.config_files) == {"server.properties", "panel.properties"}


async def test_accept_eula_clears_prompt_and_allows_launch(tmp_path, monkeypatch):
    monkeypatch.setenv("MINECRAFT_JAVA", "/usr/bin/java")
    m = make(tmp_path)
    with pytest.raises(RuntimeError, match="EULA"):
        m.launch_spec()
    assert (await m.actions()["accept_eula"]({}, lambda l: None)) == {"ok": True}
    assert m.prompts() == []
    spec = m.launch_spec()
    assert spec.argv == ["/usr/bin/java", "-Xmx3G", "-jar", "server.jar", "--nogui"] and spec.cwd == m.server_dir
    for name in ("server.properties", "eula.txt"):  # the game reads these from its folder; the real files stay on the config volume
        assert (m.server_dir / name).is_symlink() and (m.server_dir / name).resolve() == (m.config_dir / name).resolve()


def test_bad_memory_value_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("MINECRAFT_JAVA", "/usr/bin/java")
    m = make(tmp_path)
    (m.config_dir / "panel.properties").write_text("memory=lots; rm -rf /\n")
    (m.config_dir / "eula.txt").write_text("eula=true\n")
    assert m.launch_spec().argv[1] == "-Xmx2G"


async def test_vanilla_install_verifies_and_skips_when_current(tmp_path):
    hits = []
    m = make(tmp_path, "vanilla", hits)
    await m.install(lambda l: None)
    assert m.is_installed() and (m.server_dir / "server.jar").read_bytes() == JAR_BYTES
    assert json.loads((m.server_dir / MARKER).read_text())["version"] == "1.21.8"
    jar_fetches = sum("dl.test" in h for h in hits)
    logs = []
    await m.install(logs.append)  # nothing changed: no second download
    assert sum("dl.test" in h for h in hits) == jar_fetches and any("up to date" in l for l in logs)


async def test_checksum_mismatch_is_rejected_and_leaves_no_jar(tmp_path):
    m = make(tmp_path, "vanilla", bad_sum=True)
    with pytest.raises(RuntimeError, match="SHA1"):
        await m.install(lambda l: None)
    assert not m.is_installed() and not (m.server_dir / "server.jar.part").exists()


async def test_paper_picks_newest_version_with_a_stable_build(tmp_path):
    m = make(tmp_path, "paper")
    await m.install(lambda l: None)  # 1.21.8 only has an alpha build, so 1.21.7 build 31 wins; the -rc1 is ignored
    marker = json.loads((m.server_dir / MARKER).read_text())
    assert (marker["flavor"], marker["version"], marker["build"]) == ("paper", "1.21.7", "31")


async def test_switching_flavor_downloads_again(tmp_path):
    hits = []
    m = make(tmp_path, "vanilla", hits)
    await m.install(lambda l: None)
    (m.config_dir / "panel.properties").write_text("flavor=paper\nversion=latest\n")
    await m.install(lambda l: None)
    assert json.loads((m.server_dir / MARKER).read_text())["flavor"] == "paper"


def test_unknown_flavor_is_an_error(tmp_path):
    m = make(tmp_path)
    (m.config_dir / "panel.properties").write_text("flavor=forge\n")
    with pytest.raises(RuntimeError, match="unknown server type"):
        m._panel_settings()


def test_clean_keeps_worlds_and_settings_but_wipes_the_jar(tmp_path):
    m = make(tmp_path)
    (m.config_dir / "server.properties").write_text("level-name=survival\n")
    (m.config_dir / "eula.txt").write_text("eula=true\n")
    for f in ("survival/level.dat", "survival_nether/DIM-1/r.0.mca", "plugins/Essentials/config.yml", "ops.json",
              "libraries/junk.jar", "cache/x", "server.jar", "world/other.dat"):
        (m.server_dir / f).parent.mkdir(parents=True, exist_ok=True)
        (m.server_dir / f).write_text("x")
    (m.server_dir / "eula.txt").symlink_to(m.config_dir / "eula.txt")
    m.clean(lambda l: None)
    left = sorted(str(p.relative_to(m.server_dir)) for p in m.server_dir.rglob("*") if p.is_file())
    assert left == ["ops.json", "plugins/Essentials/config.yml", "survival/level.dat", "survival_nether/DIM-1/r.0.mca"]
    assert (m.config_dir / "eula.txt").read_text() == "eula=true\n" and not (m.server_dir / "eula.txt").exists()


def test_level_name_cannot_point_outside_the_server_folder(tmp_path):
    m = make(tmp_path)
    (m.config_dir / "server.properties").write_text("level-name=../../etc\n")
    assert "world" in m.persistent_paths and not any(".." in p for p in m.persistent_paths)


def test_adding_a_minecraft_server_seeds_files_and_uses_the_longer_stop_timeout(tmp_path):
    from panel.manager import ServerManager
    from panel.modules import catalog
    mgr = ServerManager(tmp_path, modules=catalog())
    assert catalog()["minecraft"].status == "available"
    srv = mgr.add("minecraft", "Family world")
    assert (srv.config_dir / "panel.properties").is_file() and (srv.config_dir / "server.properties").is_file()
    assert srv.supervisor.stop_timeout == 90.0
    assert srv.module.describe()["prompts"][0]["id"] == "eula"
