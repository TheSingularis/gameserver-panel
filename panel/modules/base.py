"""Contract every game module implements.

A module owns everything game-specific: how to install/update, how to launch,
which ports matter, which config files are editable, and any extra actions
(e.g. an account sign-in, for a game that needs one). The panel core knows nothing about any game.
"""
from __future__ import annotations

import shutil
from dataclasses import asdict, dataclass, field
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


@dataclass(frozen=True)
class ConfigField:
    """One setting in a key=value config file, shown as a form field on the Config tab."""
    key: str
    label: str
    type: str = "text"  # "text", "number", "bool", "password" or "select"
    help: str = ""
    options: tuple[str, ...] = ()  # for type "select"


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
    # Known settings per config file, for the form view. Keys in the file that are not listed here still
    # appear under "Other settings", and anything the form does not touch is left byte-for-byte as it was.
    # Syntax of the config files: "equals" for `key=value`, "space" for `key value`.
    config_format: str = "equals"
    config_schema: dict[str, list[ConfigField]] = {}
    # Paths under server_dir that hold player data (worlds, saves, bans) and must survive a clean reinstall.
    # None = not declared yet, which makes "clean & reinstall" refuse; use [] for a game with nothing to keep.
    persistent_paths: list[str] | None = None

    def __init__(self, config_dir: Path, server_dir: Path):
        self.config_dir = config_dir
        self.server_dir = server_dir
        # Set by the supervisor while install() runs; modules call it as (percent 0-100 or None, phase text).
        self.on_progress: Callable[[float | None, str], None] = lambda pct, phase: None

    async def install(self, log: LogFn) -> None:
        """Install or update the game server files. Must stream progress to log."""
        raise NotImplementedError

    def clean(self, log: LogFn) -> None:
        """Delete the installed game files so install() starts from nothing.

        Anything under persistent_paths (worlds, saves, bans...) is kept, as is the config dir. A module that
        has not declared persistent_paths cannot be cleaned: we can't know what is safe to delete.
        """
        if self.persistent_paths is None:
            raise RuntimeError(f"{self.name} has not declared which files hold saves, so it can't be cleaned safely")
        d = self.server_dir
        if not d.is_dir():
            return
        if d.is_symlink() or d == d.parent or len(d.resolve().parts) < 3:
            raise RuntimeError(f"refusing to clean {d}")
        keep = [Path(p) for p in self.persistent_paths]

        def wipe(dir_: Path, rel: Path) -> None:
            for child in dir_.iterdir():
                r = rel / child.name
                if any(r == k for k in keep):
                    continue
                if child.is_dir() and not child.is_symlink():
                    if any(r in k.parents for k in keep):  # holds something to keep: wipe around it
                        wipe(child, r)
                    else:
                        shutil.rmtree(child)
                else:
                    child.unlink()
        wipe(d, Path())
        log(f"[panel] removed game files in {d}" + (f" (kept: {', '.join(self.persistent_paths)})" if keep else ""))

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
            "config_format": self.config_format,
            "config_schema": {f: [asdict(x) for x in fs] for f, fs in self.config_schema.items()},
            "actions": sorted(self.actions()),
            "installed": self.is_installed(),
        }
