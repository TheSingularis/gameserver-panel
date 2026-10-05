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


async def test_reinstall_wipes_game_files_but_not_config(tmp_path):
    m = FakeModule(tmp_path / "config", tmp_path / "server")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "fake.cfg").write_text("keep")
    (tmp_path / "server" / "sub").mkdir(parents=True)
    (tmp_path / "server" / "sub" / "broken.dat").write_text("x")
    (tmp_path / "server" / "top.bin").write_text("x")
    m.installed = True
    s = Supervisor(m, stop_timeout=2)
    await s.start()
    await s.reinstall()
    assert sorted(p.name for p in (tmp_path / "server").iterdir()) == []
    assert (tmp_path / "config" / "fake.cfg").read_text() == "keep"
    assert m.installs == 1 and s.state == "running" and s.progress is None  # restarted afterwards
    await s.stop()


async def test_reinstall_failure_leaves_server_stopped(tmp_path):
    m = FakeModule(tmp_path / "config", tmp_path / "server")
    (tmp_path / "server").mkdir()

    async def boom(log):
        raise RuntimeError("steamcmd exited with 8")
    m.install = boom
    s = Supervisor(m, stop_timeout=2)
    with pytest.raises(RuntimeError):
        await s.reinstall()
    assert s.state == "stopped" and any("reinstall failed" in l for l in s.tail())


async def test_reinstall_keeps_declared_worlds_and_refuses_undeclared(tmp_path):
    m = FakeModule(tmp_path / "config", tmp_path / "server")
    m.persistent_paths = ["saves/world1", "players.db"]
    for f in ("saves/world1/level.dat", "saves/world2/tmp.dat", "players.db", "junk.bin", "bin/game.exe"):
        (tmp_path / "server" / f).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "server" / f).write_text("x")
    m.installed = True
    s = Supervisor(m, stop_timeout=2)
    await s.reinstall()
    left = sorted(str(p.relative_to(tmp_path / "server")) for p in (tmp_path / "server").rglob("*") if p.is_file())
    assert left == ["players.db", "saves/world1/level.dat"]

    m.persistent_paths = None  # a game that never declared what to keep
    with pytest.raises(RuntimeError, match="can't be cleaned safely"):
        await s.reinstall()
    assert (tmp_path / "server" / "players.db").exists() and s.state == "stopped"
