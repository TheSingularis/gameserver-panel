"""The Ship: Remasted dedicated server (Steam tool app 443050).

Facts (SteamDB / Steam community): Windows-only Unity server (the exe is 32-bit; SteamDB wrongly implies 64-bit), anonymous
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
MISSING_CONFIG_EXIT = 8  # steamcmd "Failed installing app (Missing configuration)"
STATE_RE = re.compile(r"Update state \(0x[0-9a-f]+\) ([a-z ]+), progress: ([\d.]+)")
SELF_UPDATE_RE = re.compile(r"\[\s*(\d+)%\]\s+(Downloading|Extracting|Installing)")
PHASES = {"downloading": "Downloading", "verifying update": "Verifying", "verifying install": "Verifying",
          "committing": "Finishing", "reconfiguring": "Preparing", "preallocating": "Preparing"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")
GUARD_RE = re.compile(r"^[A-Za-z0-9]{0,10}$")


class TheShip(GameModule):
    id = "theship"
    name = "The Ship: Remasted"
    description = "Murder-mystery hunt on a cruise ship. Dedicated server with a public listing."
    ports = [
        Port(7777, 7778, "udp", "game and query traffic (verified: forwarding these made the server public)"),
        Port(7776, 7778, "tcp", "forwarded in the verified setup; not proven necessary", required=False),
        Port(443, 443, "tcp+udp", "named in community docs; not forwarded and the server still listed", required=False),
    ]

    def __init__(self, config_dir: Path, server_dir: Path):
        super().__init__(config_dir, server_dir)
        self.server_id = os.environ.get("SHIP_SERVER_ID", "TSRDS_1")
        self.cfg_name = "server.cfg"
        self.config_files = [self.cfg_name]
        self.steamcmd = os.environ.get("STEAMCMD", "/opt/steamcmd/steamcmd.sh")

    def _home_env(self) -> dict[str, str]:
        # steamcmd keeps its login cache under $HOME/Steam; put it on the persistent volume so a
        # recreated container stays logged in.
        home = self.config_dir / "home"
        home.mkdir(parents=True, exist_ok=True)
        return {"HOME": str(home)}

    # -- install ----------------------------------------------------------
    def is_installed(self) -> bool:
        return (self.server_dir / EXE).is_file()

    def _progress_from(self, line: str) -> None:
        m = STATE_RE.search(line)
        if m:
            self.on_progress(float(m.group(2)), PHASES.get(m.group(1).strip(), m.group(1).strip().capitalize()))
            return
        m = SELF_UPDATE_RE.search(line)
        if m:
            self.on_progress(float(m.group(1)), "Updating SteamCMD")

    async def _run(self, argv: list[str], log: LogFn, env: dict | None = None) -> int:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, **(env or {})})
        assert proc.stdout
        # steamcmd redraws progress with bare \r, so split on both \r and \n.
        buf = ""
        while chunk := await proc.stdout.read(4096):
            buf += chunk.decode(errors="replace")
            *done, buf = re.split(r"[\r\n]+", buf)
            for line in done:
                if line:
                    self._progress_from(line)
                    if not STATE_RE.search(line):  # keep the log readable: progress goes to the bar
                        log(line)
        if buf:
            log(buf)
        return await proc.wait()

    async def install(self, log: LogFn) -> None:
        self.server_dir.mkdir(parents=True, exist_ok=True)
        argv = [self.steamcmd, "+@sSteamCmdForcePlatformType", "windows",
                "+force_install_dir", str(self.server_dir), "+login", "anonymous",
                "+app_update", SERVER_APP_ID, "validate", "+quit"]
        code = await self._run(argv, log, self._home_env())
        if code == MISSING_CONFIG_EXIT:
            # A cold steamcmd (no appinfo cache yet) can quit with "Missing configuration" before the app's
            # info arrives; the first run warms the cache, so one retry normally succeeds.
            log("[panel] steamcmd had no app info yet (exit 8); retrying once")
            code = await self._run(argv, log, self._home_env())
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
            **self._home_env(), "WINEPREFIX": str(self.config_dir / "wine32"), "WINEARCH": "win32", "WINEDEBUG": "-all"})

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
        code = await self._run(argv, lambda l: log(l.replace(password, "********")), self._home_env())
        return {"ok": code == 0, "exit_code": code}
