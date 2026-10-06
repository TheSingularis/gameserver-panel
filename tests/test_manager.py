from panel.manager import ServerManager
from tests.conftest import FAKE_CATALOG


def test_servers_persist_across_restart(tmp_path):
    m = ServerManager(tmp_path, modules=FAKE_CATALOG)
    m.add("fake")
    again = ServerManager(tmp_path, modules=FAKE_CATALOG)
    assert list(again.servers) == ["fake"]
    assert again.get("fake").server_dir == tmp_path / "servers" / "fake" / "server"


def test_seed_module_uses_legacy_paths(tmp_path):
    m = ServerManager(tmp_path, seed_module="fake", modules=FAKE_CATALOG)
    s = m.get("fake")
    assert (s.config_dir, s.server_dir) == (tmp_path / "config", tmp_path / "server")


def test_existing_ship_install_is_adopted_in_place(tmp_path):
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "TSRDedicated.exe").write_text("")
    m = ServerManager(tmp_path)  # real catalogue, no PANEL_MODULE
    s = m.get("theship")
    assert s.server_dir == tmp_path / "server" and s.module.is_installed()


def test_fresh_install_starts_empty(tmp_path):
    assert ServerManager(tmp_path).list() == []


async def test_remove_keeps_files(tmp_path):
    m = ServerManager(tmp_path, modules=FAKE_CATALOG)
    s = m.add("fake")
    s.server_dir.mkdir(parents=True)
    (s.server_dir / "keep.txt").write_text("x")
    await m.remove("fake")
    assert (s.server_dir / "keep.txt").exists() and m.list() == []
    assert ServerManager(tmp_path, modules=FAKE_CATALOG).list() == []


def test_several_named_servers_per_game_and_rename_persists(tmp_path):
    m = ServerManager(tmp_path, modules=FAKE_CATALOG)
    a, b, c = m.add("fake", "Friends"), m.add("fake", "Friends"), m.add("fake")
    assert [a.id, b.id, c.id] == ["friends", "friends-2", "fake"]
    assert a.server_dir != b.server_dir
    m.rename("friends-2", "Hardcore")
    again = ServerManager(tmp_path, modules=FAKE_CATALOG)
    assert [s.name for s in again.servers.values()] == ["Friends", "Hardcore", "Fake"]


def test_autostart_flag_persists(tmp_path):
    m = ServerManager(tmp_path, modules=FAKE_CATALOG)
    m.add("fake")
    assert m.get("fake").autostart is False
    m.set_autostart("fake", True)
    assert ServerManager(tmp_path, modules=FAKE_CATALOG).get("fake").autostart is True


async def _autostart(tmp_path, installed):
    """Three fake servers (a, b, c), all set to auto-start; `installed` says which have game files. Returns (manager, sleeps, started)."""
    m = ServerManager(tmp_path, modules=FAKE_CATALOG)
    for n in "abc":
        s = m.add("fake", n)
        s.module.installed = n in installed
        m.set_autostart(s.id, True)
    sleeps, started = [], []
    for s in m.servers.values():
        async def fake_start(sid=s.id): started.append(sid)
        s.supervisor.start = fake_start
    async def sleep(t): sleeps.append(t)
    await m.run_autostart(60, 30, sleep)
    return m, sleeps, started


async def test_autostart_waits_then_staggers(tmp_path):
    _, sleeps, started = await _autostart(tmp_path, "abc")
    assert sleeps == [60, 30, 30] and started == ["a", "b", "c"]


async def test_autostart_skips_uninstalled_without_a_gap(tmp_path):
    m, sleeps, started = await _autostart(tmp_path, "ac")
    assert sleeps == [60, 30] and started == ["a", "c"]
    assert "not installed" in "\n".join(m.get("b").supervisor.tail(10))


async def test_autostart_does_nothing_when_no_server_opted_in(tmp_path):
    m = ServerManager(tmp_path, modules=FAKE_CATALOG)
    m.add("fake")
    sleeps = []
    async def sleep(t): sleeps.append(t)
    await m.run_autostart(60, 30, sleep)
    assert sleeps == []


def test_theship_icon_is_the_runtime_steam_url_not_a_bundled_file():
    from panel.modules import catalog
    from panel.manager import STATIC_ICONS
    assert catalog()["theship"].icon.endswith("/community_assets/images/apps/383790/de54185795014585bc27f4649f85f926f7ea63e5.jpg")
    assert not list(STATIC_ICONS.glob("theship.*"))  # fetched by the browser at runtime, nothing bundled


def test_minecraft_icon_is_a_pinned_runtime_url_not_a_bundled_file():
    from panel.modules import catalog
    from panel.manager import STATIC_ICONS
    icon = catalog()["minecraft"].icon
    assert icon.startswith("https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons@") and icon.endswith("/png/minecraft.png")
    assert not list(STATIC_ICONS.glob("minecraft.*"))
