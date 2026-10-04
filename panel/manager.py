"""Holds the set of game servers the panel manages and persists it to <data>/servers.json.

One server per game module for now (ids are module ids). Existing single-game installs keep their
files where they are: a legacy /data/config + /data/server layout is adopted in place.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .modules import ModuleInfo, catalog
from .modules.base import GameModule
from .supervisor import Supervisor

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


@dataclass
class Server:
    id: str
    module_id: str
    config_dir: Path
    server_dir: Path
    module: GameModule
    supervisor: Supervisor

    def summary(self) -> dict:
        return {"id": self.id, "module": self.module_id, "name": self.module.name,
                "description": self.module.description, **self.supervisor.status()}


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
                entries.append({"id": seed_module, "module": seed_module, "config": "config", "server": "server"})
            self._save(entries)
        for e in entries:
            info = self.catalog.get(e["module"])
            if info and info.factory:
                self._instantiate(e["id"], info, self.data_dir / e["config"], self.data_dir / e["server"])

    def _entries(self) -> list[dict]:
        rel = lambda p: str(p.relative_to(self.data_dir))
        return [{"id": s.id, "module": s.module_id, "config": rel(s.config_dir), "server": rel(s.server_dir)}
                for s in self.servers.values()]

    def _save(self, entries: list[dict] | None = None) -> None:
        tmp = self._file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._entries() if entries is None else entries, indent=2))
        os.replace(tmp, self._file)

    def _instantiate(self, sid: str, info: ModuleInfo, config_dir: Path, server_dir: Path) -> Server:
        module = info.factory(config_dir, server_dir)
        srv = Server(sid, info.id, config_dir, server_dir, module, Supervisor(module))
        self.servers[sid] = srv
        return srv

    # ---- operations -----------------------------------------------------
    def get(self, sid: str) -> Server:
        try:
            return self.servers[sid]
        except KeyError:
            raise KeyError(f"no such server: {sid}")

    def add(self, module_id: str) -> Server:
        info = self.catalog.get(module_id)
        if not info:
            raise ValueError("unknown game")
        if not info.factory:
            raise ValueError(f"{info.name} is not available yet")
        if module_id in self.servers:
            raise ValueError(f"{info.name} is already added")
        base = self.data_dir / "servers" / module_id
        srv = self._instantiate(module_id, info, base / "config", base / "server")
        self._save()
        return srv

    async def remove(self, sid: str) -> None:
        srv = self.get(sid)
        await srv.supervisor.stop()
        del self.servers[sid]
        self._save()  # game files are left on disk on purpose

    def list(self) -> list[dict]:
        return [s.summary() for s in self.servers.values()]

    def available(self) -> list[dict]:
        return [{"id": m.id, "name": m.name, "description": m.description, "tags": list(m.tags),
                 "status": m.status, "added": m.id in self.servers} for m in self.catalog.values()]
