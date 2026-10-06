"""Holds the set of game servers the panel manages and persists it to <data>/servers.json.

A server is a named instance of a game module; several can exist for one game. The id is a slug fixed
at creation (it names the folder), the display name can be changed any time. Existing single-game
installs keep their files where they are: a legacy /data/config + /data/server layout is adopted in place.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .modules import ModuleInfo, catalog
from .modules.base import GameModule
from .supervisor import Supervisor

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
STATIC_ICONS = Path(__file__).parent / "static" / "icons"


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:28] or "server"


def clean_name(name: object, fallback: str) -> str:
    name = " ".join(str(name or "").split())[:40]
    return name or fallback


@dataclass
class Server:
    id: str
    name: str
    module_id: str
    icon: str | None
    config_dir: Path
    server_dir: Path
    module: GameModule
    supervisor: Supervisor
    autostart: bool = False  # start with the container (see ServerManager.run_autostart)

    def summary(self) -> dict:
        return {"id": self.id, "module": self.module_id, "name": self.name, "game": self.module.name,
                "icon": self.icon, "autostart": self.autostart, "description": self.module.description, **self.supervisor.status()}


class ServerManager:
    def __init__(self, data_dir: Path, demo: bool = False, seed_module: str | None = None,
                 modules: dict[str, ModuleInfo] | None = None):
        self.data_dir = data_dir
        self.catalog: dict[str, ModuleInfo] = modules if modules is not None else catalog(demo)
        self.servers: dict[str, Server] = {}
        self._file = data_dir / "servers.json"
        data_dir.mkdir(parents=True, exist_ok=True)
        self._load(seed_module)

    # ---- persistence ----------------------------------------------------
    def _load(self, seed_module: str | None) -> None:
        if self._file.exists():
            entries = json.loads(self._file.read_text())
        else:
            entries = []
            # Pre-multi-game installs kept The Ship's files in /data/server and /data/config: adopt them in place.
            if not seed_module and (self.data_dir / "server" / "TSRDedicated.exe").exists():
                seed_module = "theship"
            if seed_module and seed_module in self.catalog and self.catalog[seed_module].factory:
                entries.append({"id": seed_module, "name": self.catalog[seed_module].name, "module": seed_module,
                                "config": "config", "server": "server"})
            self._save(entries)
        for e in entries:
            info = self.catalog.get(e["module"])
            if info and info.factory:
                srv = self._instantiate(e["id"], clean_name(e.get("name"), info.name), info,
                                        self.data_dir / e["config"], self.data_dir / e["server"])
                srv.autostart = bool(e.get("autostart", False))

    def _entries(self) -> list[dict]:
        rel = lambda p: str(p.relative_to(self.data_dir))
        return [{"id": s.id, "name": s.name, "module": s.module_id, "config": rel(s.config_dir), "server": rel(s.server_dir),
                 "autostart": s.autostart}
                for s in self.servers.values()]

    def _save(self, entries: list[dict] | None = None) -> None:
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._entries() if entries is None else entries, indent=2))
        os.replace(tmp, self._file)

    def icon_for(self, info: ModuleInfo) -> str | None:
        """A bundled icon wins (works offline); otherwise the module's remote URL, if any."""
        for ext in ("png", "jpg", "svg"):
            if (STATIC_ICONS / f"{info.id}.{ext}").exists():
                return f"/static/icons/{info.id}.{ext}"
        return info.icon

    def _instantiate(self, sid: str, name: str, info: ModuleInfo, config_dir: Path, server_dir: Path) -> Server:
        module = info.factory(config_dir, server_dir)
        srv = Server(sid, name, info.id, self.icon_for(info), config_dir, server_dir, module, Supervisor(module, stop_timeout=module.stop_timeout or 20.0))
        self.servers[sid] = srv
        return srv

    # ---- operations -----------------------------------------------------
    def get(self, sid: str) -> Server:
        try:
            return self.servers[sid]
        except KeyError:
            raise KeyError(f"no such server: {sid}")

    def _new_id(self, base: str) -> str:
        sid, n = base, 2
        while sid in self.servers or (self.data_dir / "servers" / sid).exists():
            sid, n = f"{base[:26]}-{n}", n + 1
        return sid

    def add(self, module_id: str, name: str | None = None, options: dict | None = None) -> Server:
        info = self.catalog.get(module_id)
        if not info:
            raise ValueError("unknown game")
        if not info.factory:
            raise ValueError(f"{info.name} is not available yet")
        name = clean_name(name, info.name)
        sid = self._new_id(slugify(name))
        base = self.data_dir / "servers" / sid
        srv = self._instantiate(sid, name, info, base / "config", base / "server")
        try:
            srv.module.prepare(self._clean_options(srv.module, options))
        except Exception:
            del self.servers[sid]
            raise
        self._save()
        return srv

    @staticmethod
    def _clean_options(module: GameModule, options: dict | None) -> dict:
        """Keep only the choices the game declared, and only values it offers."""
        out: dict = {}
        for opt in module.create_options:
            when = opt.get("applies_when") or {}
            if any(out.get(k) not in allowed for k, allowed in when.items()):
                continue  # e.g. a modpack zip brings its own Minecraft version
            val = (options or {}).get(opt["key"], opt.get("default"))
            if "pattern" in opt:
                if not isinstance(val, str) or not re.fullmatch(opt["pattern"], val):
                    raise ValueError(f"{opt['label']}: {opt.get('hint', 'not a valid value')}")
            elif val not in opt["choices"]:
                raise ValueError(f"{opt['label']}: pick one of {', '.join(opt['choices'])}")
            out[opt["key"]] = val
        return out

    async def choices(self, module_id: str, key: str, picks: dict) -> dict:
        info = self.catalog.get(module_id)
        if not info or not info.choices or not any(o["key"] == key and o.get("choices_from") for o in info.options):
            raise ValueError("no list of choices for that")
        return await info.choices(key, picks)

    def rename(self, sid: str, name: str) -> Server:
        srv = self.get(sid)
        srv.name = clean_name(name, srv.name)
        self._save()
        return srv

    def set_autostart(self, sid: str, on: bool) -> Server:
        srv = self.get(sid)
        srv.autostart = bool(on)
        self._save()
        return srv

    async def run_autostart(self, delay: float, stagger: float, sleep=asyncio.sleep) -> None:
        """Container just came up: wait `delay`, then start each auto-start server in sidebar order, `stagger` apart.

        A server that is not installed is skipped without costing a stagger gap; one that fails to start logs the
        error to its own console and the rest still go. Runs once, so a server you stop by hand stays stopped.
        """
        queue = [s.id for s in self.servers.values() if s.autostart]
        if not queue:
            return
        await sleep(delay)
        started = False
        for sid in queue:
            srv = self.servers.get(sid)  # may have been removed or switched off during the wait
            if not srv or not srv.autostart:
                continue
            if not srv.module.is_installed():
                srv.supervisor.log("[panel] auto-start skipped: game files are not installed yet")
                continue
            if started:
                await sleep(stagger)
            started = True
            try:
                srv.supervisor.log("[panel] auto-starting with the container")
                await srv.supervisor.start()
            except Exception as e:  # keep going: one bad server must not block the rest
                srv.supervisor.log(f"[panel] auto-start failed: {e}")

    async def remove(self, sid: str) -> None:
        srv = self.get(sid)
        await srv.supervisor.stop()
        del self.servers[sid]
        self._save()  # game files are left on disk on purpose

    def list(self) -> list[dict]:
        return [s.summary() for s in self.servers.values()]

    def available(self) -> list[dict]:
        return [{"id": m.id, "name": m.name, "description": m.description, "tags": list(m.tags),
                 "status": m.status, "icon": self.icon_for(m), "options": list(m.options),
                 "servers": sum(1 for x in self.servers.values() if x.module_id == m.id)} for m in self.catalog.values()]
