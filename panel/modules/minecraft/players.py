"""Who may join a Minecraft server, who is an operator and who is banned: the four JSON lists the game keeps.

They live in the server folder and the running game rewrites them itself, so the panel changes them the way an
operator would: while the server runs it types the console command (`whitelist add`, `op`, `ban`...), and the game
does the rest, UUID lookup included. While the server is stopped it edits the file directly and looks the UUID up
itself (from Mojang, or the offline one when online-mode is off).
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

NAME = re.compile(r"^[A-Za-z0-9_.]{1,17}$")
KINDS = {  # list id -> file
    "whitelist": "whitelist.json",
    "ops": "ops.json",
    "banned-players": "banned-players.json",
    "banned-ips": "banned-ips.json",
}
PROFILE_API = "https://api.mojang.com/users/profiles/minecraft/"
BAN_REASON = "Banned by an operator."


def offline_uuid(name: str) -> str:
    """The UUID a server in offline mode gives a player: Java's UUID.nameUUIDFromBytes("OfflinePlayer:<name>")."""
    raw = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    raw[6] = (raw[6] & 0x0F) | 0x30
    raw[8] = (raw[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(raw)))


def clean_reason(text: object) -> str:
    """One line, no control characters: it ends up inside a console command, so it must not be able to start another."""
    one = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).split())
    return one[:100]


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S +0000")


class PlayersMixin:
    players = True
    _PLAYERS_TRANSPORT: httpx.AsyncBaseTransport | None = None  # tests put a fake network here

    # -- reading ------------------------------------------------------------
    def _list_path(self, kind: str) -> Path:
        if kind not in KINDS:
            raise ValueError("unknown list")
        return self.server_dir / KINDS[kind]

    def _read_list(self, kind: str) -> list[dict]:
        p = self._list_path(kind)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(errors="replace") or "[]")
        except ValueError:
            raise RuntimeError(f"{KINDS[kind]} is not valid JSON. Fix it on the server, or delete it to start the list again.") from None
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []

    def _write_list(self, kind: str, entries: list[dict]) -> None:
        p = self._list_path(kind)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=2))
        os.replace(tmp, p)

    async def player_lists(self) -> dict:
        props = self._props("server.properties")
        return {"lists": {k: self._read_list(k) for k in KINDS},
                "info": {"whitelist_enabled": props.get("white-list", "false").lower() == "true",
                         "online_mode": props.get("online-mode", "true").lower() != "false"}}

    # -- checking what was asked ---------------------------------------------
    @staticmethod
    def _subject(kind: str, value: object) -> str:
        value = str(value or "").strip()
        if kind == "banned-ips":
            try:
                return str(ipaddress.ip_address(value))
            except ValueError:
                raise ValueError("that is not an IP address") from None
        if not NAME.fullmatch(value):
            raise ValueError("a player name is 1 to 17 letters, digits, _ or .")
        return value

    # -- while the server runs: type the command ------------------------------
    def player_command(self, kind: str, action: str, value: object, extra: dict) -> str:
        subject = self._subject(kind, value)
        reason = clean_reason(extra.get("reason"))
        if kind == "whitelist" and action in ("add", "remove"):
            return f"whitelist {action} {subject}"
        if kind == "ops" and action == "add":
            return f"op {subject}"
        if kind == "ops" and action == "remove":
            return f"deop {subject}"
        if kind == "ops" and action == "set_level":
            raise ValueError("Stop the server to change an operator's level: the game only reads levels from the file at start.")
        if kind == "banned-players" and action == "add":
            return f"ban {subject} {reason}".strip()
        if kind == "banned-players" and action == "remove":
            return f"pardon {subject}"
        if kind == "banned-ips" and action == "add":
            return f"ban-ip {subject} {reason}".strip()
        if kind == "banned-ips" and action == "remove":
            return f"pardon-ip {subject}"
        raise ValueError("unknown action")

    # -- while it is stopped: edit the file -----------------------------------
    async def _uuid_for(self, name: str) -> tuple[str, str]:
        """(uuid, name as Mojang spells it). Offline-mode servers use the name-derived UUID instead."""
        if self._props("server.properties").get("online-mode", "true").lower() == "false":
            return offline_uuid(name), name
        try:
            async with httpx.AsyncClient(transport=self._PLAYERS_TRANSPORT, follow_redirects=True, timeout=httpx.Timeout(10.0)) as client:
                r = await client.get(PROFILE_API + name)
        except httpx.HTTPError:
            raise RuntimeError("Could not reach Mojang to look the player up. Check the server's internet connection.") from None
        if r.status_code in (204, 404):
            raise ValueError(f"No Minecraft account is called {name}.")
        r.raise_for_status()
        data = r.json()
        return str(uuid.UUID(data["id"])), data.get("name", name)

    async def player_edit(self, kind: str, action: str, value: object, extra: dict) -> None:
        subject = self._subject(kind, value)
        entries = self._read_list(kind)
        key = "ip" if kind == "banned-ips" else "name"
        same = lambda e: str(e.get(key, "")).lower() == subject.lower()
        if action == "remove":
            self._write_list(kind, [e for e in entries if not same(e)])
            return
        if action == "set_level" and kind == "ops":
            level = int(extra.get("level", 0))
            if level not in (1, 2, 3, 4):
                raise ValueError("an operator level is 1 to 4")
            for e in entries:
                if same(e):
                    e["level"] = level
            self._write_list(kind, entries)
            return
        if action != "add":
            raise ValueError("unknown action")
        if any(same(e) for e in entries):
            raise ValueError(f"{subject} is already on this list.")
        reason = clean_reason(extra.get("reason")) or BAN_REASON
        if kind == "banned-ips":
            entries.append({"ip": subject, "created": now_stamp(), "source": "Server", "expires": "forever", "reason": reason})
        else:
            uid, name = await self._uuid_for(subject)
            if kind == "whitelist":
                entries.append({"uuid": uid, "name": name})
            elif kind == "ops":
                level = int(self._props("server.properties").get("op-permission-level", "4") or 4)
                entries.append({"uuid": uid, "name": name, "level": level if level in (1, 2, 3, 4) else 4, "bypassesPlayerLimit": False})
            else:
                entries.append({"uuid": uid, "name": name, "created": now_stamp(), "source": "Server", "expires": "forever", "reason": reason})
        self._write_list(kind, entries)
