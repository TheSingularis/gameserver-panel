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
