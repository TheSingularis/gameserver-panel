import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from panel.app import Settings, create_app
from panel.manager import ServerManager
from panel.modules import catalog
from panel.modules import minecraft as mc
from panel.modules.minecraft import Minecraft


def lists_network(fail=False):
    def handler(req: httpx.Request) -> httpx.Response:
        url = str(req.url)
        if fail:
            return httpx.Response(503)
        if url.endswith("version_manifest_v2.json"):
            return httpx.Response(200, json={"latest": {"release": "1.21.8", "snapshot": "25w01a"}, "versions": [
                {"id": "25w01a", "type": "snapshot"}, {"id": "1.21.8", "type": "release"},
                {"id": "1.21.7", "type": "release"}, {"id": "1.20.4", "type": "release"}, {"id": "b1.7.3", "type": "old_beta"}]})
        if url == "https://fill.papermc.io/v3/projects/paper":
            return httpx.Response(200, json={"versions": {"1.21": ["1.21.7", "1.21.8", "1.21.9-rc1"], "1.20": ["1.20.4"]}})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


async def test_vanilla_list_is_releases_only_newest_first():
    got = await Minecraft.option_choices("version", {"flavor": "vanilla"}, lists_network())
    assert got == {"choices": ["1.21.8", "1.21.7", "1.20.4"], "latest": "1.21.8"}


async def test_paper_list_skips_release_candidates_and_sorts_newest_first():
    got = await Minecraft.option_choices("version", {"flavor": "paper"}, lists_network())
    assert got == {"choices": ["1.21.8", "1.21.7", "1.20.4"], "latest": "1.21.8"}


async def test_no_list_for_packs_or_other_keys():
    for key, picks in (("version", {"flavor": "pack"}), ("flavor", {"flavor": "paper"}), ("version", {})):
        with pytest.raises(ValueError):
            await Minecraft.option_choices(key, picks, lists_network())


def test_chosen_version_lands_in_panel_properties(tmp_path):
    mgr = ServerManager(tmp_path, modules=catalog())
    mgr.add("minecraft", "Old", {"flavor": "vanilla", "version": "1.20.4"})
    mgr.add("minecraft", "Default", {"flavor": "paper"})
    mgr.add("minecraft", "Mods", {"flavor": "pack", "version": "1.20.4"})  # a pack brings its own: the field is ignored
    props = lambda sid: (tmp_path / "servers" / sid / "config" / "panel.properties").read_text()
    assert "flavor=vanilla" in props("old") and "version=1.20.4" in props("old")
    assert "version=latest" in props("default")
    assert "version=latest" in props("mods")


@pytest.mark.parametrize("bad", ["", "1.21.8\nflavor=pack", "../x", "1.21.", "snapshot", None, 5])
def test_a_bad_version_is_refused(tmp_path, bad):
    mgr = ServerManager(tmp_path, modules=catalog())
    with pytest.raises(ValueError, match="Minecraft version"):
        mgr.add("minecraft", "X", {"flavor": "paper", "version": bad})
    assert not mgr.servers


@pytest.fixture
async def api(tmp_path, monkeypatch):
    mgr = ServerManager(tmp_path, modules=catalog(demo=True))
    app = create_app(Settings("correct horse", tmp_path), mgr)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, monkeypatch


async def test_choices_endpoint(api):
    c, monkeypatch = api
    assert (await c.get("/api/games/minecraft/choices/version?flavor=paper")).status_code == 401
    await c.post("/api/login", json={"password": "correct horse"})
    monkeypatch.setattr(mc, "_TEST_TRANSPORT", lists_network())
    r = await c.get("/api/games/minecraft/choices/version?flavor=vanilla")
    assert r.status_code == 200 and r.json()["latest"] == "1.21.8"
    opt = {o["key"]: o for o in (await c.get("/api/games")).json()["games"][1]["options"]}["version"]
    assert opt["applies_when"] == {"flavor": ["paper", "vanilla"]} and opt["choices_from"] is True
    assert (await c.get("/api/games/minecraft/choices/version?flavor=pack")).status_code == 404
    assert (await c.get("/api/games/demo/choices/version")).status_code == 404
    assert (await c.get("/api/games/nope/choices/version")).status_code == 404
    monkeypatch.setattr(mc, "_TEST_TRANSPORT", lists_network(fail=True))
    assert (await c.get("/api/games/minecraft/choices/version?flavor=paper")).status_code == 502
