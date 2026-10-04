"""Single-admin password auth with a signed, expiring session cookie (no extra deps)."""
from __future__ import annotations

import hmac
import hashlib
import secrets
import time
from pathlib import Path

COOKIE = "gsp_session"
TTL = 12 * 3600


def load_secret(data_dir: Path) -> bytes:
    p = data_dir / ".secret"
    if not p.exists():
        data_dir.mkdir(parents=True, exist_ok=True)
        p.write_bytes(secrets.token_bytes(32))
        p.chmod(0o600)
    return p.read_bytes()


def make_token(secret: bytes, now: float | None = None) -> str:
    exp = str(int((now or time.time()) + TTL))
    sig = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def valid_token(secret: bytes, token: str | None, now: float | None = None) -> bool:
    if not token or "." not in token:
        return False
    exp, sig = token.split(".", 1)
    good = hmac.new(secret, exp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good) and exp.isdigit() and int(exp) > (now or time.time())


class LoginLimiter:
    """Crude brute-force brake: 5 failures per client => 60s lockout."""

    def __init__(self, max_fail: int = 5, lock_s: int = 60):
        self.max_fail, self.lock_s = max_fail, lock_s
        self._fails: dict[str, list[float]] = {}

    def blocked(self, who: str) -> bool:
        now = time.time()
        recent = [t for t in self._fails.get(who, []) if now - t < self.lock_s]
        self._fails[who] = recent
        return len(recent) >= self.max_fail

    def fail(self, who: str) -> None:
        self._fails.setdefault(who, []).append(time.time())
