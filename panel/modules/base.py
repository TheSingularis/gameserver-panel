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
    readonly: bool = False  # shown as a plain label: the module refuses changes to it once the server exists
    # The page loads the choices from /api/games/<module>/choices/<key> (newest first, plus what "latest" means) and shows a
    # dropdown; it stays a text box if the list cannot be fetched.
    choices_from: bool = False
    older_warning: bool = False  # warn, and confirm on Save, when the value goes older than the saved one (versions)
    applies_when: dict = field(default_factory=dict)  # only show the field when other settings in the file match, e.g. {"flavor": ["paper"]}
    # Which tab the setting appears under. Order of first appearance in the schema is the tab order. Without a group the page
    # falls back to the file's own section banners (as The Ship's server.cfg has), then to "Other settings" at the end.
    group: str = ""
    optional: bool = False  # only offered when the file already has the key (the game may add it itself later)


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
    # Seconds the game gets to shut down cleanly before it is killed; None = the panel's default.
    stop_timeout: float | None = None
    # Choices asked for when a server of this game is created, e.g. [{"key": "flavor", "label": "Server type",
    # "choices": ["paper", "vanilla"], "default": "paper"}]; passed to prepare().
    create_options: list[dict] = []
    # File types the game takes as an upload on the server page (e.g. ".zip"); None = no upload.
    upload_accept: str | None = None
    # True when the running game reads admin commands from its stdin; the console then shows a command box.
    console_input: bool = False
    # True when the game keeps lists of players (who may join, operators, bans) the Players tab can manage; a module that
    # sets it implements player_lists(), player_command() and player_edit() (see minecraft/players.py).
    players: bool = False

    def content_kinds(self) -> list[dict]:
        """Uploadable content the game manages on its own tab (datapacks, plugins): [{id, label, accept}]. A module that
        returns any implements content_list(), content_add() and content_change() (see minecraft/content.py)."""
        return []

    def __init__(self, config_dir: Path, server_dir: Path):
        self.config_dir = config_dir
        self.server_dir = server_dir
        # Set by the supervisor while install() runs; modules call it as (percent 0-100 or None, phase text).
        self.on_progress: Callable[[float | None, str], None] = lambda pct, phase: None

    def prepare(self, options: dict | None = None) -> None:
        """Called when a server of this game is created (with the user's create_options) and again before install/launch;
        write default settings files here, never overwrite existing ones."""

    def check_config(self, name: str, old: str, new: str) -> None:
        """Called before a config file is saved with its current and new text; raise ValueError to refuse the change."""

    def accept_upload(self, path: Path, log: LogFn) -> None:
        """Take an uploaded file (already saved at `path`, deleted afterwards). Runs in a worker thread."""
        raise RuntimeError(f"{self.name} does not take uploads")

    def reinstall_blocker(self) -> str | None:
        """Why a clean reinstall must be refused right now, or None when it is allowed."""
        if self.persistent_paths is None:
            return f"{self.name} has not declared which files hold saves, so it can't be cleaned safely"
        return None

    def prompts(self) -> list[dict]:
        """Things the user must do before the server can run. Each: {id, text, button, action, link?}; the UI shows them
        above the console and calls POST .../actions/<action> when the button is pressed."""
        return []

    async def install(self, log: LogFn) -> None:
        """Install or update the game server files. Must stream progress to log."""
        raise NotImplementedError

    def clean(self, log: LogFn) -> None:
        """Delete the installed game files so install() starts from nothing.

        Anything under persistent_paths (worlds, saves, bans...) is kept, as is the config dir. A module that
        has not declared persistent_paths cannot be cleaned: we can't know what is safe to delete.
        """
        blocker = self.reinstall_blocker()
        if blocker:
            raise RuntimeError(blocker)
        self.wipe(self.persistent_paths or [], log)

    def wipe(self, keep_paths: list[str], log: LogFn) -> None:
        """Delete everything in server_dir except the given relative paths."""
        d = self.server_dir
        if not d.is_dir():
            return
        if d.is_symlink() or d == d.parent or len(d.resolve().parts) < 3:
            raise RuntimeError(f"refusing to clean {d}")
        keep = [Path(p) for p in keep_paths]

        def walk(dir_: Path, rel: Path) -> None:
            for child in dir_.iterdir():
                r = rel / child.name
                if any(r == k for k in keep):
                    continue
                if child.is_dir() and not child.is_symlink():
                    if any(r in k.parents for k in keep):  # holds something to keep: wipe around it
                        walk(child, r)
                    else:
                        shutil.rmtree(child)
                else:
                    child.unlink()
        walk(d, Path())
        log(f"[panel] removed game files in {d}" + (f" (kept: {', '.join(keep_paths)})" if keep else ""))

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
            "prompts": self.prompts(),
            "upload_accept": self.upload_accept,
            "console_input": self.console_input,
            "players": self.players,
            "content": self.content_kinds(),
            "can_reinstall": self.reinstall_blocker() is None,
        }
