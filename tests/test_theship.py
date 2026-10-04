from panel.modules.theship import TheShip


def test_launch_spec_matches_steamdb_args(tmp_path, monkeypatch):
    monkeypatch.setattr(TheShip, "_find_wine", staticmethod(lambda: "/usr/bin/wine"))
    m = TheShip(tmp_path / "c", tmp_path / "s")
    m.server_dir.mkdir()
    (m.server_dir / "TSRDedicated.exe").write_text("")
    assert m.is_installed()
    spec = m.launch_spec()
    assert spec.argv[-8:] == ["-batchmode", "-nographics", "+serverid", "TSRDS_1",
                              "+servercfg", "server.cfg", "-logFile", "TSRDS_1/tsrds_output.txt"]
    assert (m.server_dir / "steam_appid.txt").read_text().strip() == "383790"
    assert spec.env["HOME"] == str(m.config_dir / "home")  # steam login cache lives on the volume


def test_config_seeded_from_shipped_then_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(TheShip, "_find_wine", staticmethod(lambda: "/usr/bin/wine"))
    m = TheShip(tmp_path / "c", tmp_path / "s")
    (m.server_dir / "TSRDS_1").mkdir(parents=True)
    (m.server_dir / "TSRDS_1" / "server.cfg").write_text("shipped")
    m.launch_spec()
    assert (m.config_dir / "server.cfg").read_text() == "shipped"
    (m.config_dir / "server.cfg").write_text("edited")  # user edit survives an update
    (m.server_dir / "TSRDS_1" / "server.cfg").write_text("shipped-v2")
    m.launch_spec()
    assert (m.server_dir / "TSRDS_1" / "server.cfg").read_text() == "edited"


async def test_steam_login_validates_and_redacts(tmp_path):
    import stat
    fake = tmp_path / "steamcmd.sh"
    fake.write_text("#!/bin/sh\necho \"args: $@\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    m = TheShip(tmp_path / "c", tmp_path / "s")
    m.steamcmd = str(fake)
    logs = []
    import pytest
    with pytest.raises(ValueError):
        await m._steam_login({"username": "a b", "password": "x"}, logs.append)
    r = await m._steam_login({"username": "jordan", "password": "hunter2"}, logs.append)
    assert r["ok"]
    assert not any("hunter2" in l for l in logs)


def test_find_wine_prefers_available_binary_and_errors_clearly(monkeypatch):
    import pytest
    import shutil
    monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/wine" if c == "wine" else None)
    assert TheShip._find_wine() == "/usr/bin/wine"
    monkeypatch.setattr(shutil, "which", lambda c: None)
    with pytest.raises(RuntimeError, match="wine not found"):
        TheShip._find_wine()
