"""Which ports is something actually listening on? Reads /proc/net so it needs no extra tools.

Inside the container this answers "is the game server bound?", which Docker's host port
mapping cannot (the host side listens whether or not the game does).
"""
from __future__ import annotations

from pathlib import Path

PROC = Path("/proc/net")
TCP_LISTEN = "0A"


def _ports(text: str, only_state: str | None) -> set[int]:
    found: set[int] = set()
    for line in text.splitlines()[1:]:
        f = line.split()
        if len(f) < 4 or ":" not in f[1]:
            continue
        if only_state and f[3] != only_state:
            continue
        found.add(int(f[1].rsplit(":", 1)[1], 16))
    return found


def listening(proc: Path = PROC) -> dict[str, set[int]]:
    def read(name: str, state: str | None) -> set[int]:
        out: set[int] = set()
        for suffix in ("", "6"):
            try:
                out |= _ports((proc / f"{name}{suffix}").read_text(), state)
            except OSError:
                pass
        return out

    return {"tcp": read("tcp", TCP_LISTEN), "udp": read("udp", None)}


def port_status(specs, proc: Path = PROC, mine_running: bool = True) -> list[dict]:
    """One row per port. `listening` means this server holds it; a port bound while this server is not running is `taken`
    (by another server or some other program), so one server never looks healthy because another holds its port."""
    bound = listening(proc)
    rows = []
    for spec in specs:
        for proto in spec.proto.split("+"):
            for port in range(spec.start, spec.end + 1):
                up = port in bound[proto]
                rows.append({"port": port, "proto": proto, "required": spec.required,
                             "note": spec.note, "listening": up and mine_running, "taken": up and not mine_running})
    return rows
