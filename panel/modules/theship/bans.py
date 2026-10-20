"""The Ship's ban list: banned_user.cfg in the server's folder, plus the server console's ban commands.

The file looks like this (CRLF line endings, the player's name sits in a comment above each ban):

    // BanList
    // steamid<TAB>duration<TAB>end_time
    // Some Player
    76561198033914776 60 2026-10-20T01:43:47:736

The running game keeps its bans in memory and only the console changes them live (`banid_steamid`, `removeid_steamid`,
`removeid_all`). So while the server runs the panel types the command and also writes the same change to the file, which
keeps the list on screen right and survives a restart; while it is stopped it only edits the file.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

STEAMID = re.compile(r"^\d{17}$")
ENTRY = re.compile(r"^(\d{17})\s+(\d+)\s+(\S+)\s*$")
HEADER = ("// BanList", "// steamid\tduration\tend_time")
MAX_MINUTES = 60 * 24 * 365


def _ends(stamp: str) -> str | None:
    """The file's `2026-10-20T01:43:47:736` (UTC, milliseconds after a colon) as ISO `2026-10-20T01:43:47.736Z`."""
    m = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?::(\d{1,3}))?", stamp)
    return f"{m.group(1)}.{(m.group(2) or '0').ljust(3, '0')}Z" if m else None


def _stamp(ends: str | None) -> str:
    """ISO `2026-10-20T01:43:47.736Z` back to the file's form; a permanent ban (no end) gets the zero date."""
    return f"{ends[:19]}:{ends[20:23]}" if ends else "0001-01-01T00:00:00:000"


class BansMixin:
    players = True
    console_input = True  # the game's own console takes the ban commands; the panel pipes its stdin
    edit_file_when_live = True
    suggests_players = False  # no name boxes: a ban is by Steam ID

    def _bans_path(self):
        return self.server_dir / self.server_id / "banned_user.cfg"

    def _read_bans(self) -> list[dict]:
        p = self._bans_path()
        if not p.exists():
            return []
        out, name = [], ""
        for raw in p.read_text(errors="replace").splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("//"):
                if line not in (h.strip() for h in HEADER) and not re.fullmatch(r"//\s*steamid\s+duration\s+end_time", line):
                    name = line[2:].strip()
                continue
            m = ENTRY.match(line)
            if m:
                minutes = int(m.group(2))
                out.append({"steamid": m.group(1), "name": name, "minutes": minutes, "ends": _ends(m.group(3)) if minutes else None})
            name = ""
        return out

    def _write_bans(self, bans: list[dict]) -> None:
        lines = list(HEADER)
        for b in bans:
            lines += [f"// {b['name'] or b['steamid']}", f"{b['steamid']} {b['minutes']} {_stamp(b['ends'])}"]
        p = self._bans_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(("\r\n".join(lines) + "\r\n").encode())
        os.replace(tmp, p)

    # -- what the Players tab asks of a game ------------------------------------
    async def player_lists(self) -> dict:
        return {"lists": {"bans": self._read_bans()},
                "info": {"views": [["bans", "Banned"]], "layout": "ship",
                         "stopped_text": "The server is stopped: changes are saved to the ban list and apply when it starts."}}

    @staticmethod
    def _steamid(value: object) -> str:
        value = str(value or "").strip()
        if not STEAMID.fullmatch(value):
            raise ValueError("a Steam ID is 17 digits, like 76561198033914776")
        return value

    @staticmethod
    def _minutes(extra: dict) -> int:
        try:
            minutes = int(extra.get("minutes", 60))
        except (TypeError, ValueError):
            raise ValueError("minutes is a whole number, or 0 for a permanent ban") from None
        if not 0 <= minutes <= MAX_MINUTES:
            raise ValueError("minutes is 0 (permanent) to 525600 (a year)")
        return minutes

    def player_command(self, kind: str, action: str, value: object, extra: dict) -> str:
        if kind != "bans":
            raise ValueError("unknown list")
        if action == "clear":
            return "removeid_all"
        sid = self._steamid(value)
        if action == "add":
            return f"banid_steamid {sid} {self._minutes(extra)}"
        if action == "remove":
            return f"removeid_steamid {sid}"
        raise ValueError("unknown action")

    async def player_edit(self, kind: str, action: str, value: object, extra: dict) -> None:
        if kind != "bans":
            raise ValueError("unknown list")
        bans = self._read_bans()
        if action == "clear":
            self._write_bans([])
            return
        sid = self._steamid(value)
        if action == "remove":
            self._write_bans([b for b in bans if b["steamid"] != sid])
            return
        if action != "add":
            raise ValueError("unknown action")
        minutes = self._minutes(extra)
        ends = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S.000Z") if minutes else None
        self._write_bans([b for b in bans if b["steamid"] != sid] + [{"steamid": sid, "name": "", "minutes": minutes, "ends": ends}])

    def online_players(self, console_lines: list[str]):
        return None

    def known_players(self) -> list[str]:
        return []
