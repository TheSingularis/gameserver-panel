import asyncio

import pytest

from panel.supervisor import Supervisor
from tests.conftest import FakeModule


@pytest.fixture
def sup(tmp_path):
    m = FakeModule(tmp_path / "c", tmp_path)
    m.installed = True
    return Supervisor(m, stop_timeout=2)


async def wait_for(cond, t=5):
    for _ in range(int(t * 50)):
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timeout")


async def test_start_logs_and_stop(sup):
    await sup.start()
    assert sup.state == "running"
    await wait_for(lambda: "hello" in sup.tail())
    await sup.stop()
    assert sup.state == "stopped"


async def test_crash_detected(tmp_path):
    m = FakeModule(tmp_path / "c", tmp_path, script="import sys;sys.exit(3)")
    m.installed = True
    s = Supervisor(m)
    await s.start()
    await wait_for(lambda: s.state == "crashed")
    assert s.exit_code == 3


async def test_start_requires_install_and_not_twice(tmp_path, sup):
    s = Supervisor(FakeModule(tmp_path / "c", tmp_path))
    with pytest.raises(RuntimeError, match="not installed"):
        await s.start()
    await sup.start()
    with pytest.raises(RuntimeError):
        await sup.start()
    await sup.stop()


async def test_stop_kills_stubborn_process(tmp_path):
    script = "import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);print('up',flush=True);time.sleep(60)"
    m = FakeModule(tmp_path / "c", tmp_path, script=script)
    m.installed = True
    s = Supervisor(m, stop_timeout=0.5)
    await s.start()
    await wait_for(lambda: "up" in s.tail())
    await s.stop()
    assert s.state == "stopped"
    assert any("timed out" in l for l in s.tail())


async def test_update_installs_and_restarts(sup):
    await sup.start()
    await sup.update()
    assert sup.module.installs == 1
    assert sup.state == "running"
    await sup.stop()


async def test_restart(sup):
    await sup.start()
    pid = sup.status()["pid"]
    await sup.restart()
    assert sup.status()["pid"] != pid
    await sup.stop()


async def test_update_reports_progress_then_clears(tmp_path):
    from tests.conftest import FakeModule
    m = FakeModule(tmp_path / "c", tmp_path)
    s = Supervisor(m)
    seen = []
    orig = m.install

    async def spy(log):
        await orig(log)
        seen.append(dict(s.progress))

    m.install = spy
    await s.update()
    assert seen == [{"pct": 50.0, "phase": "Downloading"}]
    assert s.progress is None and s.status()["progress"] is None


async def test_reinstall_wipes_game_files_but_keeps_config_and_kept_paths(tmp_path):
    server, config = tmp_path / "server", tmp_path / "config"
    (server / "bin").mkdir(parents=True)
    (server / "saves" / "world1").mkdir(parents=True)
    (server / "bin" / "game.exe").write_text("corrupt")
    (server / "saves" / "world1" / "level.dat").write_text("precious")
    (server / "junk.tmp").write_text("x")
    config.mkdir()
    (config / "fake.cfg").write_text("settings")
    m = FakeModule(config, server)
    m.keep, m.installed = ["saves"], True
    s = Supervisor(m)
    await s.reinstall(restart_after=False)
    assert not (server / "bin").exists() and not (server / "junk.tmp").exists()
    assert (server / "saves" / "world1" / "level.dat").read_text() == "precious"
    assert (config / "fake.cfg").read_text() == "settings"
    assert m.installs == 1 and s.state == "stopped"


async def test_reinstall_refused_unless_module_declares_what_to_keep(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    (server / "world.dat").write_text("precious")
    m = FakeModule(tmp_path / "config", server)  # keep is None
    s = Supervisor(m)
    with pytest.raises(RuntimeError, match="does not support"):
        await s.reinstall()
    assert (server / "world.dat").exists() and m.installs == 0 and s.state == "stopped"


async def test_reinstall_refuses_server_dir_that_contains_config(tmp_path):
    m = FakeModule(tmp_path / "c", tmp_path)  # config inside server dir
    m.keep = []
    with pytest.raises(RuntimeError, match="refusing"):
        await Supervisor(m).reinstall()
    assert (tmp_path).exists()


async def test_reinstall_keeps_nested_path_and_wipes_its_siblings(tmp_path):
    server = tmp_path / "server"
    (server / "data" / "worlds").mkdir(parents=True)
    (server / "data" / "cache").mkdir()
    (server / "data" / "worlds" / "w.dat").write_text("precious")
    (server / "data" / "cache" / "c").write_text("x")
    m = FakeModule(tmp_path / "config", server)
    m.keep = ["data/worlds"]
    await Supervisor(m).reinstall(restart_after=False)
    assert (server / "data" / "worlds" / "w.dat").read_text() == "precious"
    assert not (server / "data" / "cache").exists()
