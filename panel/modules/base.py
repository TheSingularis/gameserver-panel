"""Contract every game module implements.

A module owns everything game-specific: how to install/update, how to launch,
which ports matter, which config files are editable, and any extra actions
(e.g. The Ship's Steam login). The panel core knows nothing about any game.
"""
from __future__ import annotations

import shutil
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
    # Paths under server_dir that clean() keeps (worlds, saves). None = clean reinstall not supported.
    keep: list[str] | None = None

    def __init__(self, config_dir: Path, server_dir: Path):
        self.config_dir = config_dir
        self.server_dir = server_dir
        # Set by the supervisor while install() runs; modules call it as (percent 0-100 or None, phase text).
        self.on_progress: Callable[[float | None, str], None] = lambda pct, phase: None

    async def install(self, log: LogFn) -> None:
        """Install or update the game server files. Must stream progress to log."""
        raise NotImplementedError

    def clean(self) -> None:
        """Delete the downloaded game files so install() starts from nothing.

        Opt-in per game: a module sets `keep` to the paths (relative to server_dir) that hold player data
        such as worlds or saves, which survive the wipe. `keep = None` (the default) means the module has not
        said what is safe to delete, so reinstalling is refused. config_dir is never touched.
        """
        if self.keep is None:
            raise RuntimeError(f"{self.name} does not support clean reinstall")
        server, config = self.server_dir.resolve(), self.config_dir.resolve()
        if server == server.parent or server == config or server in config.parents:
            raise RuntimeError(f"refusing to clean {server}: it is a filesystem root or holds the config")
        if not server.is_dir():
            return
        keep = {(server / k).resolve() for k in self.keep}
        if any(server not in k.parents for k in keep):
            raise RuntimeError("keep paths must be inside the server directory")
        self._wipe(server, keep)

    @staticmethod
    def _wipe(directory: Path, keep: set[Path]) -> None:
        for child in directory.iterdir():  # contents only: server_dir itself may be a mount point
            resolved = child.resolve() if not child.is_symlink() else child.absolute()
            if resolved in keep:
                continue
            if child.is_dir() and not child.is_symlink():
                if any(resolved in k.parents for k in keep):  # holds something to keep: go inside
                    GameModule._wipe(child, keep)
                else:
                    shutil.rmtree(child)
            else:
                child.unlink()

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
            "can_reinstall": self.keep is not None,
        }
