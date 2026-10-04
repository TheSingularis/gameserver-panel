"""The Ship: Remasted dedicated server (Steam tool app 443050).

Facts (SteamDB / Steam community): Windows-only 64-bit Unity server, anonymous
download, launch args `-batchmode -nographics +serverid X +servercfg server.cfg`,
steam_appid.txt must be 383790, default ports TCP/UDP 7776-7778 and 443.
Everything Steam-account related (login) lives here, not in the panel core.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
from pathlib import Path

from ..base import GameModule, LaunchSpec, LogFn, Port

SERVER_APP_ID = "443050"
GAME_APP_ID = "383790"  # steam_appid.txt must stay this
EXE = "TSRDedicated.exe"
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")
GUARD_RE = re.compile(r"^[A-Za-z0-9]{0,10}$")


class TheShip(GameModule):
    id = "theship"
    name = "The Ship: Remasted"
    ports = [
        Port(7776, 7778, "tcp+udp", "game traffic (default game port 7777, set in server.cfg)"),
        Port(443, 443, "tcp+udp", "used by the server per community docs"),
    ]

    def __init__(self, config_dir: Path, server_dir: Path):
        super().__init__(config_dir, server_dir)
        self.server_id = os.environ.get("SHIP_SERVER_ID", "TSRDS_1")
        self.cfg_name = "server.cfg"
        self.config_files = [self.cfg_name]
        self.steamcmd = os.environ.get("STEAMCMD", "/opt/steamcmd/steamcmd.sh")

    # -- install ----------------------------------------------------------
    def is_installed(self) -> bool:
        return (self.server_dir / EXE).is_file()

    async def _run(self, argv: list[str], log: LogFn, env: dict | None = None) -> int:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **(env or {})})
        assert proc.stdout
        async for raw in proc.stdout:
            log(raw.decode(errors="replace"))
        return await proc.wait()

    async def install(self, log: LogFn) -> None:
        self.server_dir.mkdir(parents=True, exist_ok=True)
        code = await self._run([
            self.steamcmd, "+@sSteamCmdForcePlatformType", "windows",
            "+force_install_dir", str(self.server_dir), "+login", "anonymous",
            "+app_update", SERVER_APP_ID, "validate", "+quit"], log)
        if code != 0:
            raise RuntimeError(f"steamcmd exited with {code}")
        (self.server_dir / "steam_appid.txt").write_text(GAME_APP_ID + "\n")

    # -- launch -----------------------------------------------------------
    def _sync_config(self) -> None:
        """Keep server.cfg on the persistent config volume; seed it from the shipped default."""
        mine = self.config_dir / self.cfg_name
        shipped = [self.server_dir / self.server_id / self.cfg_name, self.server_dir / self.cfg_name]
        self.config_dir.mkdir(parents=True, exist_ok=True)
        if not mine.exists():
            for src in shipped:
                if src.is_file():
                    shutil.copyfile(src, mine)
                    break
        if mine.exists():
            for dst in shipped:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(mine, dst)

    @staticmethod
    def _find_wine() -> str:
        # TSRDedicated.exe is 32-bit, so use Debian's `wine` wrapper (needs wine32:i386); wine64 is only a fallback.
        for cand in ("wine", "wine64", "/usr/lib/wine/wine64"):
            found = shutil.which(cand)
            if found:
                return found
        raise RuntimeError("wine not found in this container (tried wine64, wine, /usr/lib/wine/wine64)")

    def launch_spec(self) -> LaunchSpec:
        wine = self._find_wine()
        self._sync_config()
        (self.server_dir / "steam_appid.txt").write_text(GAME_APP_ID + "\n")
        (self.server_dir / self.server_id).mkdir(exist_ok=True)
        argv = ["xvfb-run", "-a", wine, EXE, "-batchmode", "-nographics",
                "+serverid", self.server_id, "+servercfg", self.cfg_name,
                "-logFile", f"{self.server_id}/tsrds_output.txt"]
        return LaunchSpec(argv, self.server_dir, {
            "WINEPREFIX": str(self.config_dir / "wine32"), "WINEARCH": "win32", "WINEDEBUG": "-all"})

    # -- steam login (UNVERIFIED that this satisfies the game's SteamAPI_Init) ----
    def actions(self):
        return {"steam_login": self._steam_login}

    async def _steam_login(self, params: dict, log: LogFn) -> dict:
        user = str(params.get("username", ""))
        password = str(params.get("password", ""))
        guard = str(params.get("guard_code", ""))
        if not USERNAME_RE.match(user) or not password or not GUARD_RE.match(guard):
            raise ValueError("invalid username, password or guard code")
        argv = [self.steamcmd, "+login", user, password] + ([guard] if guard else []) + ["+quit"]
        # Never log argv: it contains the password.
        log(f"[steam] logging in as {user} (credentials cached by steamcmd on success)")
        code = await self._run(argv, lambda l: log(l.replace(password, "********")))
        return {"ok": code == 0, "exit_code": code}
