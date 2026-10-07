"""Version changes, the locked server type, and what an update or a clean reinstall may touch."""
import hashlib
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from panel.app import Settings, create_app
from panel.manager import ServerManager
from panel.modules import catalog
from panel.modules import minecraft as mc
from panel.modules.minecraft import MARKER, Minecraft

JAR = b"PK-fake-jar" * 50
SHA = hashlib.sha256(JAR).hexdigest()


def build(i, channel):
    return {"id": i, "channel": channel, "downloads": {"server:default": {"url": f"https://dl.test/{i}.jar", "checksums": {"sha256": SHA}}}}


def paper_network():
    """26.3 only has pre-release builds (as on 2026-10-07); 26.2 and 1.21.8 have stable ones."""
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if url == "https://fill.papermc.io/v3/projects/paper":
            return httpx.Response(200, json={"versions": {"26.3": ["26.3", "26.3-rc-3"], "26.2": ["26.2"], "1.21": ["1.21.8"]}})
        if url.endswith("/versions/26.3/builds"):
            return httpx.Response(200, json=[build(159, "BETA"), build(158, "BETA"), build(200, "ALPHA"), build(7, "ALPHA")])
        if url.endswith("/versions/26.2/builds") or url.endswith("/versions/1.21.8/builds"):
            return httpx.Response(200, json=[build(30, "STABLE"), build(31, "STABLE"), build(40, "BETA")])
        if url.startswith("https://dl.test/"):
            return httpx.Response(200, content=JAR, headers={"content-length": str(len(JAR))})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def make(tmp_path, version, flavor="paper"):
    m = Minecraft(tmp_path / "config", tmp_path / "server")
    m.prepare()
    (m.config_dir / "panel.properties").write_text(f"flavor={flavor}\nversion={version}\nmemory=2G\n")
    m._transport = paper_network()
    return m


async def test_latest_skips_a_version_with_only_prerelease_builds(tmp_path):
    m = make(tmp_path, "latest")
    await m.install(lambda l: None)
    marker = json.loads((m.server_dir / MARKER).read_text())
    assert (marker["version"], marker["build"]) == ("26.2", "31")


async def test_an_exact_version_with_no_stable_build_uses_its_newest_beta_and_says_so(tmp_path):
    m = make(tmp_path, "26.3")
    log = []
    await m.install(log.append)
    marker = json.loads((m.server_dir / MARKER).read_text())
    assert (marker["version"], marker["build"]) == ("26.3", "159")  # the newest BETA, not the higher-numbered ALPHA
    assert any("no stable build for 26.3" in l and "beta" in l for l in log)


async def test_the_paper_list_offers_26_3_but_makes_latest_the_newest_stable_one():
    got = await Minecraft.option_choices("version", {"flavor": "paper"}, paper_network())
    assert got == {"choices": ["26.3", "26.2", "1.21.8"], "latest": "26.2", "experimental": ["26.3"]}


async def test_changing_the_version_and_updating_swaps_the_jar_and_keeps_the_world(tmp_path):
    m = make(tmp_path, "1.21.8")
    await m.install(lambda l: None)
    for f in ("world/level.dat", "world_nether/DIM-1/r.0.mca", "plugins/x/config.yml", "ops.json", "spigot.yml"):
        (m.server_dir / f).parent.mkdir(parents=True, exist_ok=True)
        (m.server_dir / f).write_text("keep me")
    (m.config_dir / "panel.properties").write_text("flavor=paper\nversion=26.2\nmemory=2G\n")
    await m.install(lambda l: None)
    assert json.loads((m.server_dir / MARKER).read_text())["version"] == "26.2"
    assert (m.server_dir / "world/level.dat").read_text() == "keep me" and (m.server_dir / "world_nether/DIM-1/r.0.mca").exists()


def test_clean_reinstall_keeps_every_world_even_after_the_world_name_changed(tmp_path):
    m = make(tmp_path, "1.21.8")
    (m.config_dir / "server.properties").write_text("level-name=newworld\n")
    for f in ("oldworld/level.dat", "oldworld/region/r.0.0.mca", "newworld/level.dat", "bukkit.yml", "spigot.yml",
              "server-icon.png", "server.jar", "libraries/a.jar", "cache/x", "junk/readme.txt"):
        (m.server_dir / f).parent.mkdir(parents=True, exist_ok=True)
        (m.server_dir / f).write_text("x")
    m.clean(lambda l: None)
    left = sorted(str(p.relative_to(m.server_dir)) for p in m.server_dir.rglob("*") if p.is_file())
    assert left == ["bukkit.yml", "newworld/level.dat", "oldworld/level.dat", "oldworld/region/r.0.0.mca", "server-icon.png", "spigot.yml"]


def test_the_server_type_cannot_be_changed_but_the_version_can(tmp_path):
    m = make(tmp_path, "latest")
    old = (m.config_dir / "panel.properties").read_text()
    m.check_config("panel.properties", old, old.replace("version=latest", "version=26.2"))  # fine
    m.check_config("server.properties", "a=1\n", "a=2\n")  # other files are not looked at
    for new in (old.replace("flavor=paper", "flavor=vanilla"), old.replace("flavor=paper\n", "")  .replace("memory", "flavor=pack\nmemory")):
        with pytest.raises(ValueError, match="can't be changed"):
            m.check_config("panel.properties", old, new)
    assert m.describe()["config_schema"]["panel.properties"][0]["readonly"] is True


@pytest.fixture
async def api(tmp_path):
    mgr = ServerManager(tmp_path, modules=catalog())
    mgr.add("minecraft", "Family", {"flavor": "paper", "version": "latest"})
    app = create_app(Settings("correct horse", tmp_path), mgr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post("/api/login", json={"password": "correct horse"})
        yield c


async def test_the_api_refuses_a_flavor_change_but_saves_a_version_change(api):
    url = "/api/servers/family/config/panel.properties"
    text = (await api.get(url)).json()["content"]
    r = await api.put(url, json={"content": text.replace("flavor=paper", "flavor=vanilla")})
    assert r.status_code == 400 and "can't be changed" in r.json()["detail"]
    assert "flavor=paper" in (await api.get(url)).json()["content"]
    assert (await api.put(url, json={"content": text.replace("version=latest", "version=26.2")})).status_code == 200
    assert "version=26.2" in (await api.get(url)).json()["content"]
