"""A harmless fake game for trying the panel without downloading anything.

Enabled with PANEL_DEMO=1. "Installing" takes a few seconds and reports progress; "running" binds
UDP 27000 and prints a heartbeat, so the ports table, console and progress bar all have something to show.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from .base import ConfigField, GameModule, LaunchSpec, LogFn, Port

SCRIPT = (
    "import socket,time,sys\n"
    "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM);s.bind(('0.0.0.0',27000))\n"
    "print('Demo server listening on UDP 27000',flush=True)\n"
    "n=0\n"
    "while True:\n"
    "    time.sleep(2);n+=1;print(f'[demo] tick {n}: 0 players connected',flush=True)\n"
)


class Demo(GameModule):
    id = "demo"
    name = "Demo game"
    description = "A fake server for trying out the panel. Installs in seconds and prints a heartbeat."
    ports = [Port(27000, 27000, "udp", "demo only")]
    config_files = ["demo.cfg"]
    config_schema = {"demo.cfg": [
        ConfigField("name", "Server name", help="Shown in the server list"),
        ConfigField("max_players", "Max players", "number"),
    ]}

    def is_installed(self) -> bool:
        return (self.server_dir / "demo.installed").exists()

    async def install(self, log: LogFn) -> None:
        self.server_dir.mkdir(parents=True, exist_ok=True)
        log("Demo install starting")
        for i in range(1, 101):
            phase = "Downloading" if i <= 70 else "Verifying"
            self.on_progress(float(i), phase)
            if i % 25 == 0:
                log(f"{phase}... {i}%")
            await asyncio.sleep(0.05)
        (self.server_dir / "demo.installed").write_text("ok\n")
        log("Demo install complete")

    def launch_spec(self) -> LaunchSpec:
        self.config_dir.mkdir(parents=True, exist_ok=True)
        cfg = self.config_dir / "demo.cfg"
        if not cfg.exists():
            cfg.write_text("name=Demo server\nmax_players=8\n")
        return LaunchSpec([sys.executable, "-c", SCRIPT], Path(self.server_dir))
