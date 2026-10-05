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


def test_find_wine_prefers_available_binary_and_errors_clearly(monkeypatch):
    import pytest
    import shutil
    monkeypatch.setattr(shutil, "which", lambda c: "/usr/bin/wine" if c == "wine" else None)
    assert TheShip._find_wine() == "/usr/bin/wine"
    monkeypatch.setattr(shutil, "which", lambda c: None)
    with pytest.raises(RuntimeError, match="wine not found"):
        TheShip._find_wine()


async def test_steamcmd_progress_is_parsed_and_kept_out_of_the_log(tmp_path):
    import stat
    fake = tmp_path / "steamcmd.sh"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '[ 40%%] Downloading update (1 of 2 KB)...\\n'\n"
        "printf 'Update state (0x61) downloading, progress: 12.50 (100 / 800)\\r'\n"
        "printf 'Update state (0x5) verifying install, progress: 99.00 (790 / 800)\\r\\n'\n"
        "echo \"Success! App '443050' fully installed.\"\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    m = TheShip(tmp_path / "c", tmp_path / "s")
    m.steamcmd = str(fake)
    progress, logs = [], []
    m.on_progress = lambda pct, phase: progress.append((pct, phase))
    await m.install(logs.append)
    assert progress == [(40.0, "Updating SteamCMD"), (12.5, "Downloading"), (99.0, "Verifying")]
    assert any("Success!" in l for l in logs) and not any("Update state" in l for l in logs)


async def test_install_retries_once_on_missing_configuration(tmp_path):
    import stat
    fake = tmp_path / "steamcmd.sh"
    marker = tmp_path / "ran"
    fake.write_text(f"#!/bin/sh\nif [ -e {marker} ]; then echo ok; exit 0; fi\ntouch {marker}\nexit 8\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    m = TheShip(tmp_path / "c", tmp_path / "s")
    m.steamcmd = str(fake)
    logs = []
    await m.install(logs.append)
    assert any("retrying once" in l for l in logs)


async def test_install_does_not_retry_other_failures(tmp_path):
    import pytest
    import stat
    fake = tmp_path / "steamcmd.sh"
    fake.write_text("#!/bin/sh\nexit 5\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    m = TheShip(tmp_path / "c", tmp_path / "s")
    m.steamcmd = str(fake)
    with pytest.raises(RuntimeError, match="exited with 5"):
        await m.install(lambda l: None)


def test_clean_keeps_ban_list_and_map_cycle(tmp_path):
    m = TheShip(tmp_path / "c", tmp_path / "s")
    d = m.server_dir / "TSRDS_1"
    d.mkdir(parents=True)
    for f in ("banned_user.cfg", "mapcycle.txt", "server.cfg"):
        (d / f).write_text("x")
    (m.server_dir / "TSRDedicated.exe").write_text("x")
    m.clean(lambda l: None)
    assert sorted(p.name for p in m.server_dir.rglob("*") if p.is_file()) == ["banned_user.cfg", "mapcycle.txt"]


def test_describe_reports_space_separated_config(tmp_path):
    d = TheShip(tmp_path / "c", tmp_path / "s").describe()
    assert d["config_format"] == "space"
    assert {"hostname", "sv_password", "maxplayers"} <= {f["key"] for f in d["config_schema"]["server.cfg"]}
