"""Runs one game server as a child process, independent of the web UI.

The UI runs on the asyncio loop; the game is a separate OS process, so a busy or
crashed game never blocks the panel (and vice versa).
"""
from __future__ import annotations

import asyncio
import collections
import contextlib
import time
from typing import AsyncIterator

from .modules.base import GameModule

STOPPED, INSTALLING, RUNNING, STOPPING, CRASHED = (
    "stopped", "installing", "running", "stopping", "crashed")


class Supervisor:
    def __init__(self, module: GameModule, stop_timeout: float = 20.0, max_lines: int = 2000):
        self.module = module
        self.stop_timeout = stop_timeout
        self.state = STOPPED
        self.started_at: float | None = None
        self.exit_code: int | None = None
        self.progress: dict | None = None  # {"pct": float | None, "phase": str} while installing
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._lines: collections.deque[str] = collections.deque(maxlen=max_lines)
        self._subs: set[asyncio.Queue] = set()
        self._lock = asyncio.Lock()  # serialises start/stop/update
        self._want_running = False

    # ---- logging -------------------------------------------------------
    def log(self, line: str) -> None:
        line = line.rstrip("\n")
        self._lines.append(line)
        for q in list(self._subs):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(line)

    def tail(self, n: int = 200) -> list[str]:
        return list(self._lines)[-n:]

    async def follow(self) -> AsyncIterator[str]:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs.discard(q)

    # ---- state ---------------------------------------------------------
    def status(self) -> dict:
        up = int(time.time() - self.started_at) if self.state == RUNNING and self.started_at else None
        return {"state": self.state, "progress": self.progress, "pid": self._proc.pid if self._proc and self.state == RUNNING else None,
                "uptime_s": up, "exit_code": self.exit_code, "installed": self.module.is_installed()}

    def _set_progress(self, pct: float | None, phase: str) -> None:
        if self.state == INSTALLING:
            self.progress = {"pct": None if pct is None else round(max(0.0, min(100.0, pct)), 1), "phase": phase}

    # ---- control -------------------------------------------------------
    async def start(self) -> None:
        async with self._lock:
            await self._start_locked()

    async def _start_locked(self) -> None:
        if self.state in (RUNNING, INSTALLING):
            raise RuntimeError(f"server is {self.state}")
        if not self.module.is_installed():
            raise RuntimeError("server is not installed yet; run update first")
        spec = self.module.launch_spec()
        spec.cwd.mkdir(parents=True, exist_ok=True)
        self.log(f"$ {' '.join(spec.argv)}")
        import os
        self._proc = await asyncio.create_subprocess_exec(
            *spec.argv, cwd=spec.cwd, env={**os.environ, **spec.env},
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)  # own process group so stop() reaches wine/xvfb children
        self.state, self.started_at, self.exit_code = RUNNING, time.time(), None
        self._reader = asyncio.create_task(self._pump(self._proc))

    async def _pump(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout
        async for raw in proc.stdout:
            self.log(raw.decode(errors="replace"))
        code = await proc.wait()
        if self._proc is proc and self.state == RUNNING:
            self.exit_code, self.state = code, (STOPPED if code == 0 else CRASHED)
            self.log(f"[panel] server exited with code {code}")

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        proc = self._proc
        if not proc or self.state != RUNNING:
            return
        import os, signal
        self.state = STOPPING
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), self.stop_timeout)
        except asyncio.TimeoutError:
            self.log("[panel] graceful stop timed out, killing")
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGKILL)
            await proc.wait()
        if self._reader:
            await self._reader
        self.exit_code, self.state = proc.returncode, STOPPED
        self.log("[panel] server stopped")

    async def restart(self) -> None:
        async with self._lock:
            await self._stop_locked()
            await self._start_locked()

    async def upload(self, path) -> None:
        """Hand an uploaded file to the game module. The server must be stopped: an upload replaces game files."""
        async with self._lock:
            if self.state not in (STOPPED, CRASHED):
                raise RuntimeError(f"stop the server first (it is {self.state})")
            self.state = INSTALLING
            self.progress = {"pct": None, "phase": "Unpacking"}
            self.module.on_progress = self._set_progress
            try:
                await asyncio.to_thread(self.module.accept_upload, path, self.log)
            except Exception as e:
                self.log(f"[panel] upload failed: {e}")
                raise
            finally:
                self.state, self.progress = STOPPED, None

    async def update(self, restart_after: bool = True) -> None:
        await self._install(restart_after, clean=False)

    async def reinstall(self, restart_after: bool = True) -> None:
        """Delete the game files and install from scratch (for corrupt or half-written installs)."""
        await self._install(restart_after, clean=True)

    async def _install(self, restart_after: bool, clean: bool) -> None:
        async with self._lock:
            blocker = self.module.reinstall_blocker() if clean else None
            if blocker:  # refuse before stopping a running server
                raise RuntimeError(blocker)
            was_running = self.state == RUNNING
            await self._stop_locked()
            self.state = INSTALLING
            self.progress = {"pct": None, "phase": "Cleaning" if clean else "Starting"}
            self.module.on_progress = self._set_progress
            try:
                if clean:
                    self.module.clean(self.log)
                    self.progress = {"pct": None, "phase": "Starting"}
                await self.module.install(self.log)
            except Exception as e:
                self.log(f"[panel] {'reinstall' if clean else 'update'} failed: {e}")
                self.state, self.progress = STOPPED, None
                raise
            self.state, self.progress = STOPPED, None
            if restart_after and was_running:
                await self._start_locked()
