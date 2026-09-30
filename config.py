"""config.py — Environment / mode resolution for the Toss Trader MCP server.

Loads BYOK credentials and runtime flags from environment (and an optional
``.env`` file next to this module). Determines the operating MODE exactly once
per process via :func:`load_config`.

SAFETY MODEL
------------
- Default mode is ALWAYS ``mock``. ``live`` requires explicit opt-in
  (``TOSS_LIVE=1``) *and* a non-empty client_secret *and* the kill switch off.
- Write/order tools are only LIVE-executable when ``TOSS_ALLOW_LIVE_ORDERS=1``.
  Even then, every order call additionally requires a ``confirm`` argument and a
  fresh re-check of the kill switch.
- ``client_secret`` is never logged, never returned in any tool payload, never
  written to a fixture. ``redact()`` is provided for safe diagnostics.

No third-party dependencies — pure stdlib so the module imports under any
Python 3.9+ even before the ``mcp`` SDK is installed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

# ---------------------------------------------------------------------------
# .env loading (minimal, dependency-free)
# ---------------------------------------------------------------------------

_ENV_LOADED = False


def _load_dotenv(path: Path) -> None:
    """Populate os.environ from a simple KEY=VALUE .env file.

    Existing environment variables always win (so `TOSS_X=1 python server.py`
    overrides the file). Lines starting with '#' and blank lines are ignored.
    Never raises — a malformed/missing .env must not break startup.
    """
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    try:
        if not path.is_file():
            return
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip an inline comment ( ' #...' / tab+#) on UNQUOTED values so a
            # stray comment can't silently corrupt a numeric safety limit
            # (e.g. `TOSS_MAX_ORDER_KRW=500000  # 50만` -> "500000"). Quoted values
            # and secrets (no whitespace) are unaffected.
            if value[:1] not in ('"', "'"):
                for sep in (" #", "\t#"):
                    i = value.find(sep)
                    if i != -1:
                        value = value[:i].rstrip()
            value = value.strip('"').strip("'")
            # Do not clobber a value already present in the real environment.
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:
        # Configuration must be resilient; ignore any parse error.
        return


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _pos_float(value: Optional[str], default: float) -> float:
    """Parse a strictly-positive float env value; fall back to default.

    A non-positive or unparseable value degrades to the conservative default
    so a typo can never *widen* a safety limit (fail-closed).
    """
    try:
        v = float(str(value).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _pos_int(value: Optional[str], default: int) -> int:
    try:
        v = int(str(value).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Redaction helpers (used by logging + anywhere a secret might leak)
# ---------------------------------------------------------------------------

_SECRET_KEYS = ("secret", "token", "authorization", "password", "client_secret")


def redact(value: Optional[str]) -> str:
    """Return a non-reversible, log-safe placeholder for a sensitive string."""
    if not value:
        return "<unset>"
    # Show only that *something* is set, plus its length bucket. Never the value.
    return f"<redacted:{len(value)}chars>"


def redact_mapping(data: Dict[str, object]) -> Dict[str, object]:
    """Shallow-copy a dict, masking any obviously-sensitive key."""
    out: Dict[str, object] = {}
    for k, v in data.items():
        if any(s in k.lower() for s in _SECRET_KEYS):
            out[k] = redact(str(v) if v is not None else None)
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Config:
    """Immutable, process-lifetime configuration snapshot."""

    base_url: str
    client_id: Optional[str]
    client_secret: Optional[str] = field(repr=False)  # never repr the secret
    account_index: int
    live: bool                 # resolved: live transport vs mock
    allow_live_orders: bool    # write tools may hit the network
    kill: bool                 # global kill switch
    data_dir: Path
    stock_dashboard_dir: Path

    # ----- invariant 8: order limits (KRW, conservative defaults) -----------
    max_order_krw: float       # per-order notional ceiling (hard-fail above)
    max_daily_krw: float       # cumulative daily notional ceiling
    max_daily_count: int       # cumulative daily order count ceiling

    # ----- invariant 13A: token-at-rest persistence (opt-in) ---------------
    token_persist: bool        # write OAuth token to disk (default: memory-only)

    # ----- derived helpers -------------------------------------------------
    @property
    def mode(self) -> str:
        return "live" if self.live else "mock"

    @property
    def has_credentials(self) -> bool:
        return bool(self.client_id) and bool(self.client_secret)

    def public_summary(self) -> Dict[str, object]:
        """A secret-free dict safe to log or surface in diagnostics."""
        return {
            "base_url": self.base_url,
            "mode": self.mode,
            "allow_live_orders": self.allow_live_orders,
            "kill": self.kill,
            "account_index": self.account_index,
            "client_id_set": bool(self.client_id),
            "client_secret_set": bool(self.client_secret),
            "data_dir": str(self.data_dir),
            "max_order_krw": self.max_order_krw,
            "max_daily_krw": self.max_daily_krw,
            "max_daily_count": self.max_daily_count,
            "token_persist": self.token_persist,
        }

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            "Config("
            f"mode={self.mode!r}, base_url={self.base_url!r}, "
            f"client_id_set={bool(self.client_id)}, "
            f"client_secret={redact(self.client_secret)}, "
            f"allow_live_orders={self.allow_live_orders}, kill={self.kill})"
        )


_DEFAULT_BASE_URL = "https://openapi.tossinvest.com"


def load_config(env_path: Optional[Path] = None) -> Config:
    """Resolve configuration & operating mode exactly once.

    Mode rule (intentionally conservative)::

        live = TOSS_LIVE==1  AND  client_secret present  AND  TOSS_KILL != 1

    Anything missing or ambiguous degrades to ``mock`` — the safe default.
    """
    here = Path(__file__).resolve().parent
    _load_dotenv(env_path or (here / ".env"))

    client_id = os.environ.get("TOSS_CLIENT_ID") or None
    client_secret = os.environ.get("TOSS_CLIENT_SECRET") or None
    kill = _truthy(os.environ.get("TOSS_KILL"))
    want_live = _truthy(os.environ.get("TOSS_LIVE"))
    allow_live_orders = _truthy(os.environ.get("TOSS_ALLOW_LIVE_ORDERS"))
    token_persist = _truthy(os.environ.get("TOSS_TOKEN_PERSIST"))

    # Invariant 8 limits — conservative defaults (per skill: 100만 / 300만 / 20).
    max_order_krw = _pos_float(os.environ.get("TOSS_MAX_ORDER_KRW"), 1_000_000.0)
    max_daily_krw = _pos_float(os.environ.get("TOSS_MAX_DAILY_KRW"), 3_000_000.0)
    max_daily_count = _pos_int(os.environ.get("TOSS_MAX_DAILY_COUNT"), 20)

    # The single source of truth for transport mode.
    live = want_live and bool(client_secret) and not kill

    try:
        account_index = int(os.environ.get("TOSS_ACCOUNT_INDEX", "0"))
    except ValueError:
        account_index = 0

    data_dir = Path(
        os.environ.get("TOSS_DATA_DIR", str(Path.home() / ".toss-trader"))
    ).expanduser()
    stock_dir = Path(
        os.environ.get(
            "STOCK_DASHBOARD_DIR", str(Path.home() / "stock-dashboard")
        )
    ).expanduser()

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        # Best-effort tighten perms on the secret-bearing data dir.
        os.chmod(data_dir, 0o700)
    except Exception:
        pass

    return Config(
        base_url=os.environ.get("TOSS_BASE_URL", _DEFAULT_BASE_URL),
        client_id=client_id,
        client_secret=client_secret,
        account_index=account_index,
        live=live,
        allow_live_orders=allow_live_orders,
        kill=kill,
        data_dir=data_dir,
        stock_dashboard_dir=stock_dir,
        max_order_krw=max_order_krw,
        max_daily_krw=max_daily_krw,
        max_daily_count=max_daily_count,
        token_persist=token_persist,
    )


def kill_active() -> bool:
    """Re-read the kill switch from the live environment.

    Called immediately before EVERY order POST so an operator can disable
    trading without restarting the process (cache-bypass requirement).
    """
    return _truthy(os.environ.get("TOSS_KILL"))


if __name__ == "__main__":  # pragma: no cover - manual smoke
    cfg = load_config()
    import json

    print(json.dumps(cfg.public_summary(), ensure_ascii=False, indent=2))
