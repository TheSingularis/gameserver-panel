"""Contract every game module implements.

A module owns everything game-specific: how to install/update, how to launch,
which ports matter, which config files are editable, and any extra actions
(e.g. The Ship's Steam login). The panel core knows nothing about any game.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

LogFn = Callable[[str], None]


@dataclass(frozen=True)
class Port:
    start: int
    end: int
    proto: str  # "tcp", "udp" or "tcp+udp"
    note: str = ""
    required: bool = True  # False = forwarded in some setups but not proven necessary


@dataclass
class LaunchSpec:
    argv: list[str]
    cwd: Path
    env: dict[str, str] = field(default_factory=dict)


class GameModule:
    id: str = ""
    name: str = ""
    description: str = ""
    ports: list[Port] = []
    # Editable config files, relative to the module's config dir.
    config_files: list[str] = []

    def __init__(self, config_dir: Path, server_dir: Path):
        self.config_dir = config_dir
        self.server_dir = server_dir
        # Set by the supervisor while install() runs; modules call it as (percent 0-100 or None, phase text).
        self.on_progress: Callable[[float | None, str], None] = lambda pct, phase: None

    async def install(self, log: LogFn) -> None:
        """Install or update the game server files. Must stream progress to log."""
        raise NotImplementedError

    def launch_spec(self) -> LaunchSpec:
        raise NotImplementedError

    def is_installed(self) -> bool:
        raise NotImplementedError

    def actions(self) -> dict[str, Callable[[dict, LogFn], Awaitable[dict]]]:
        """Module-specific actions exposed at POST /api/actions/{name}."""
        return {}

    def describe(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "ports": [p.__dict__ for p in self.ports],
            "config_files": self.config_files,
            "actions": sorted(self.actions()),
            "installed": self.is_installed(),
        }
