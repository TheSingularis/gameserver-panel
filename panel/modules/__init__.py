from __future__ import annotations

from pathlib import Path

from .base import GameModule


def load_module(module_id: str, config_dir: Path, server_dir: Path) -> GameModule:
    # Explicit registry: adding a game = one import + one line here.
    if module_id == "theship":
        from .theship import TheShip

        return TheShip(config_dir, server_dir)
    raise ValueError(f"unknown module {module_id!r}")


AVAILABLE = ["theship"]
