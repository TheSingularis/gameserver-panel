"""Catalogue of game modules the panel knows about.

Adding a game = a new module class plus one entry in `catalog()`. Entries with no factory are
roadmap placeholders shown on the Add game page as "coming soon".
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .base import GameModule


@dataclass(frozen=True)
class ModuleInfo:
    id: str
    name: str
    description: str
    tags: tuple[str, ...] = ()
    factory: Callable[[Path, Path], GameModule] | None = None  # None = coming soon

    @property
    def status(self) -> str:
        return "available" if self.factory else "soon"


def _ship(config_dir: Path, server_dir: Path) -> GameModule:
    from .theship import TheShip
    return TheShip(config_dir, server_dir)


def _demo(config_dir: Path, server_dir: Path) -> GameModule:
    from .demo import Demo
    return Demo(config_dir, server_dir)


def catalog(demo: bool = False) -> dict[str, ModuleInfo]:
    items = [
        ModuleInfo("theship", "The Ship: Remasted",
                   "Murder-mystery hunt on a cruise ship. Dedicated server with a public listing; runs the Windows build under Wine.",
                   ("Steam", "Windows via Wine", "UDP 7777"), _ship),
        ModuleInfo("minecraft", "Minecraft: Java Edition",
                   "Vanilla and modded Java servers with world backups.",
                   ("Java", "TCP 25565")),
    ]
    if demo:
        items.append(ModuleInfo("demo", "Demo game",
                                "A fake server for trying out the panel. Installs in seconds and prints a heartbeat.",
                                ("Test only",), _demo))
    return {m.id: m for m in items}
