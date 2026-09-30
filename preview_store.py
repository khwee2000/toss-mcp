"""preview_store.py — One-shot frozen order previews (PLAN -> EXECUTE bridge).

A ``plan_*`` tool freezes the validated order intent into an immutable snapshot
and returns a ``preview_token``. The matching ``*_confirmed`` execute tool can
only act on a valid, unexpired, unconsumed token — never on free-form order
args. This blocks replay, stale-price, and parameter-tampering attacks.

- ``clientOrderId`` is derived deterministically from the intent hash (time
  independent) so a POST that times out can be reconciled / is idempotent.
- TTL defaults to 120s, clamped down to an FX ``validUntil`` when relevant.
- Tokens are single-use. When order execution is enabled the store is mirrored
  to a 0600 JSON file so a restart doesn't silently drop in-flight previews.

Pure stdlib.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Dict, Optional

_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


def derive_client_order_id(intent: Dict) -> str:
    """Deterministic base32 client order id (~28 chars) from the intent."""
    basis = json.dumps(
        {k: intent.get(k) for k in
         ("symbol", "side", "orderType", "quantity", "orderAmount", "price",
          "timeInForce", "account_index", "slice_index")},
        sort_keys=True, ensure_ascii=False,
    )
    digest = hashlib.sha256(basis.encode("utf-8")).digest()
    b32 = base64.b32encode(digest).decode("ascii").rstrip("=")
    coid = b32[:28]
    # Validate length & charset (defensive).
    assert 1 <= len(coid) <= 36 and all(ch in _B32 for ch in coid)
    return coid


class PreviewStore:
    def __init__(self, data_dir: Path, persist: bool = False):
        self._lock = threading.Lock()
        self._store: Dict[str, Dict] = {}
        self._persist = persist
        self._path = data_dir / "preview.json"
        if persist:
            self._load()

    def _load(self) -> None:
        try:
            if self._path.is_file():
                self._store = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self._store = {}

    def _flush(self) -> None:
        if not self._persist:
            return
        try:
            # Atomic 0600 write (O_NOFOLLOW temp + rename) — a crash mid-write
            # can no longer leave a truncated/corrupt preview file (inv 13 hygiene).
            import safety
            safety.atomic_write_0600(self._path, json.dumps(self._store))
        except Exception:
            pass

    def put(self, snapshot: Dict, ttl_seconds: float = 120.0) -> str:
        token = "pvw_" + secrets.token_urlsafe(18)
        snapshot = dict(snapshot)
        snapshot["clientOrderId"] = derive_client_order_id(snapshot)
        snapshot["confirm_phrase"] = _confirm_phrase(snapshot)
        record = {
            "snapshot": snapshot,
            "expires_at": time.time() + max(1.0, ttl_seconds),
            "consumed": False,
        }
        with self._lock:
            self._store[token] = record
            self._flush()
        return token

    def peek(self, token: str) -> Optional[Dict]:
        with self._lock:
            rec = self._store.get(token)
            if not rec:
                return None
            if rec["consumed"] or time.time() > rec["expires_at"]:
                return None
            return dict(rec["snapshot"])

    def consume(self, token: str, confirm_phrase: str) -> Dict:
        with self._lock:
            rec = self._store.get(token)
            if not rec:
                raise KeyError("unknown or already-removed preview_token")
            if rec["consumed"]:
                raise KeyError("preview_token already consumed")
            if time.time() > rec["expires_at"]:
                raise KeyError("preview_token expired — re-run the plan tool")
            expected = rec["snapshot"].get("confirm_phrase")
            if confirm_phrase != expected:
                raise PermissionError(
                    f"confirm_phrase mismatch — expected exactly: {expected!r}")
            rec["consumed"] = True
            self._flush()
            return dict(rec["snapshot"])


def _confirm_phrase(snapshot: Dict) -> str:
    """The exact phrase a human must re-type to execute (anti-autoconfirm)."""
    side = snapshot.get("side", "?")
    sym = snapshot.get("symbol", "?")
    if snapshot.get("batch"):
        return f"{side} {sym} BATCH {snapshot.get('slices', '?')}건 실행"
    if side == "CANCEL":
        return f"CANCEL {sym} {snapshot.get('orderId', '?')} 실행"
    qty = snapshot.get("quantity")
    amt = snapshot.get("orderAmount")
    size = f"{qty}주" if qty is not None else f"{amt}금액"
    return f"{side} {sym} {size} 실행"
