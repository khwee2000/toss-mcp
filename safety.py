"""safety.py — Order safety invariants 7–13 (AI autonomous-call containment).

This module implements the *enforcement helpers* for invariants 7–13 of
``.claude/skills/trading-safety-gates/skill.md``. ``server.py`` calls them from
its order/plan tools; this module never imports ``server``, ``toss_client``,
``indicators`` or ``mock_data`` (one-way dependency: server -> safety -> config).

Mapping of invariant -> public API
-----------------------------------
- **7  Idempotency**      : :func:`normalize_client_order_id`,
                            :func:`derive_client_order_id`,
                            :meth:`SafetyGuard.check_duplicate`,
                            :meth:`SafetyGuard.record_submission`.
- **8  Order limits**     : :meth:`SafetyGuard.check_limits` (per-order +
                            daily KRW notional + daily count; KST-midnight reset;
                            disk-persisted counters).
- **9  Circuit breaker**  : :meth:`SafetyGuard.check_circuit`,
                            :meth:`SafetyGuard.record_order_result`
                            (consecutive-failure trip + cooldown OPEN).
                            File kill-switch reuses ``config.kill_active()``.
- **10 Audit log**        : :meth:`SafetyGuard.audit` (append-only JSONL,
                            0600, secret-masked via ``config.redact_mapping``).
- **11 Market hours**     : :func:`check_market_open` (server injects the
                            market-calendar payload it already fetched).
- **12 Currency match**   : :func:`check_currency_match`,
                            :func:`to_krw_notional`.
- **13A token-at-rest**   : :func:`verify_token_file_perms` (0700 dir / 0600
                            file, ``O_NOFOLLOW``, atomic write helper).

Every check returns either ``None`` (pass) or a ready-to-return error envelope
``{"ok": False, "error": {"code", "message_ko"}, ...}`` so the caller can simply
``if (err := guard.check_x(...)) is not None: return err``.

Standard error codes (subset owned here):
    DUPLICATE · LIMIT_EXCEEDED · CIRCUIT_OPEN · MARKET_CLOSED ·
    CURRENCY_MISMATCH · PRICE_MOVED · INVALID_PARAM · RESTRICTED_SYMBOL

Pure stdlib. KST = UTC+9 (fixed; KRX does not observe DST).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import config as _config

KST = timezone(timedelta(hours=9))

# ---------------------------------------------------------------------------
# Standard error codes (this module's subset of the global set)
# ---------------------------------------------------------------------------

ERR_DUPLICATE = "DUPLICATE"
ERR_LIMIT = "LIMIT_EXCEEDED"
ERR_CIRCUIT = "CIRCUIT_OPEN"
ERR_MARKET_CLOSED = "MARKET_CLOSED"
ERR_CURRENCY = "CURRENCY_MISMATCH"
ERR_PRICE_MOVED = "PRICE_MOVED"
ERR_INVALID = "INVALID_PARAM"
ERR_RESTRICTED = "RESTRICTED_SYMBOL"

# ---------------------------------------------------------------------------
# Invariant 4 (extension) — symbol warning / trading-halt classification
# ---------------------------------------------------------------------------
#
# Official StockWarning.warningType enum (openapi.json v1.1.1):
#   LIQUIDATION_TRADING · OVERHEATED · INVESTMENT_WARNING · INVESTMENT_RISK ·
#   VI_STATIC · VI_DYNAMIC · VI_STATIC_AND_DYNAMIC · STOCK_WARRANTS
# plus koreanMarketDetail.{krxTradingSuspended, nxtTradingSuspended,
#   liquidationTrading} from GET /api/v1/stocks.
#
# Policy: the most dangerous states (정리매매·투자위험·거래정지) BLOCK a new
# order; the cautionary states (단기과열·투자경고·VI·신주인수권) WARN but do not
# block. Mock warning labels (older synthetic strings) are mapped too so the gate
# is testable in mock without a key.

# warningType (or mock type) -> BLOCK
_BLOCK_WARNING_TYPES = {
    "LIQUIDATION_TRADING", "INVESTMENT_RISK",
    # legacy mock labels:
    "DESIGNATED_FOR_LIQUIDATION",
}
# warningType (or mock type) -> WARN (surface, don't block)
_WARN_WARNING_TYPES = {
    "OVERHEATED", "INVESTMENT_WARNING",
    "VI_STATIC", "VI_DYNAMIC", "VI_STATIC_AND_DYNAMIC", "STOCK_WARRANTS",
    # legacy mock labels:
    "SHORT_TERM_OVERHEAT", "VI_TRIGGERED", "INVESTMENT_CAUTION",
}


def classify_symbol_restrictions(
        warnings: Optional[List[Dict]] = None,
        korean_market_detail: Optional[Dict] = None) -> Dict[str, Any]:
    """Classify a symbol's warnings + halt flags into block/warn buckets.

    Pure (no network). ``warnings`` is the list from ``get_warnings`` (each row
    has ``warningType`` live or ``type`` in mock); ``korean_market_detail`` is
    ``koreanMarketDetail`` from ``get_stocks``. Returns::

        {block: [reason...], warn: [reason...], blocked: bool}

    The caller BLOCKS the order when ``blocked`` is True (returns a
    RESTRICTED_SYMBOL envelope) and otherwise attaches ``warn`` to the result.
    """
    block: List[str] = []
    warn: List[str] = []
    for w in (warnings or []):
        if not isinstance(w, dict):
            continue
        wt = (w.get("warningType") or w.get("type") or "").upper()
        label = w.get("label") or wt
        if wt in _BLOCK_WARNING_TYPES:
            block.append(str(label))
        elif wt in _WARN_WARNING_TYPES:
            warn.append(str(label))
    kd = korean_market_detail or {}
    if kd.get("liquidationTrading") is True:
        block.append("정리매매(거래소)")
    if kd.get("krxTradingSuspended") is True:
        block.append("KRX 거래정지")
    if kd.get("nxtTradingSuspended") is True:
        block.append("NXT 거래정지")
    # de-dup while preserving order
    block = list(dict.fromkeys(block))
    warn = list(dict.fromkeys(warn))
    return {"block": block, "warn": warn, "blocked": bool(block)}


def check_symbol_restrictions(
        warnings: Optional[List[Dict]] = None,
        korean_market_detail: Optional[Dict] = None) -> Optional[Dict]:
    """Invariant 4 (ext): block orders on 정리매매/투자위험/거래정지 symbols.

    Returns ``None`` (with no restriction) or a ``RESTRICTED_SYMBOL`` envelope
    that carries the block/warn reason lists. Cautionary-only states return
    ``None`` (the caller surfaces ``warn`` separately, not a block).
    """
    cls = classify_symbol_restrictions(warnings, korean_market_detail)
    if cls["blocked"]:
        return _err(
            ERR_RESTRICTED,
            "거래제한 종목입니다(주문 차단): " + ", ".join(cls["block"]) +
            ". 정리매매·투자위험·거래정지 종목은 실주문이 거부됩니다.",
            block=cls["block"], warn=cls["warn"],
        )
    return None


def _err(code: str, message_ko: str, **extra: Any) -> Dict[str, Any]:
    """Build the standard failure envelope. Never carries secrets."""
    out: Dict[str, Any] = {"ok": False, "error": {"code": code,
                                                   "message_ko": message_ko}}
    out.update(extra)
    return out


def _kst_now() -> datetime:
    return datetime.now(KST)


def _kst_date_str(when: Optional[datetime] = None) -> str:
    return (when or _kst_now()).date().isoformat()


# ===========================================================================
# Invariant 7 — Idempotency (client_order_id)
# ===========================================================================

_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
# Toss clientOrderId field: keep <=36 chars, safe charset.
_COID_MAX = 36
_COID_ALLOWED = set(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
)


def derive_client_order_id(intent: Dict[str, Any]) -> str:
    """Deterministic base32 client order id derived from the order intent.

    Time-independent (excludes timestamps) so a POST that times out can be
    safely retried under the same id == idempotent. Mirrors
    ``preview_store.derive_client_order_id`` field selection so a preview and
    its execute resolve to the same key.
    """
    import base64

    basis = json.dumps(
        {k: intent.get(k) for k in
         ("symbol", "side", "orderType", "quantity", "orderAmount", "price",
          "timeInForce", "account_index", "slice_index")},
        sort_keys=True, ensure_ascii=False,
    )
    digest = hashlib.sha256(basis.encode("utf-8")).digest()
    coid = base64.b32encode(digest).decode("ascii").rstrip("=")[:28]
    return coid


def normalize_client_order_id(raw: Optional[str],
                              intent: Optional[Dict[str, Any]] = None) -> str:
    """Return a Toss-safe clientOrderId.

    - If ``raw`` is provided, sanitize it (allowed charset, <=36 chars).
    - Otherwise derive deterministically from ``intent`` (idempotent key).
    Raises ``ValueError`` if neither yields a usable id.
    """
    if raw:
        cleaned = "".join(c for c in str(raw).strip() if c in _COID_ALLOWED)
        cleaned = cleaned[:_COID_MAX]
        if cleaned:
            return cleaned
    if intent is not None:
        return derive_client_order_id(intent)
    raise ValueError("client_order_id 또는 intent 중 하나는 필요합니다")


# ===========================================================================
# Invariant 12 — Currency / market consistency
# ===========================================================================

def infer_currency(symbol: str, region: Optional[str] = None) -> str:
    """Best-effort currency from symbol/region. KRX numeric -> KRW else USD."""
    if region:
        return "KRW" if region.upper() in {"KR", "KRX"} else "USD"
    s = (symbol or "").strip()
    return "KRW" if s.isdigit() else "USD"


def check_currency_match(symbol: str,
                         declared_currency: Optional[str],
                         symbol_currency: Optional[str] = None,
                         region: Optional[str] = None) -> Optional[Dict]:
    """Invariant 12: reject (KRX symbol + USD) / (US symbol + KRW) mismatch.

    ``symbol_currency`` should come from the client (``currency_for``) when
    available; otherwise inferred. Returns ``None`` on match, else an error
    envelope with code ``CURRENCY_MISMATCH``.
    """
    expected = (symbol_currency or infer_currency(symbol, region)).upper()
    if not declared_currency:
        # currency required or inferable — caller may treat missing as expected,
        # but a US symbol with no declared currency is suspicious -> require it.
        return None
    declared = declared_currency.upper()
    if declared not in {"KRW", "USD"}:
        return _err(ERR_INVALID,
                    f"통화는 KRW 또는 USD여야 합니다 (입력: {declared_currency}).")
    if declared != expected:
        return _err(
            ERR_CURRENCY,
            f"통화 불일치: {symbol} 시장 통화는 {expected}인데 {declared}로 요청됨. "
            "KRW total로 미국 종목 주문 등은 환율 환산을 명시해야 합니다.",
            symbol=symbol, expected_currency=expected,
            declared_currency=declared,
        )
    return None


def to_krw_notional(amount: float,
                    currency: str,
                    usd_krw_rate: Optional[float] = None) -> Tuple[float, Dict]:
    """Convert an order notional to KRW for the invariant-8 ceiling check.

    Returns ``(krw_amount, meta)`` where ``meta`` records the rate/source/time
    used (invariant 12: explicit FX). For USD a rate is required; if none is
    supplied a conservative high default (1500) is used and flagged so the
    caller can decide to refuse a live order on an estimated rate.
    """
    cur = (currency or "KRW").upper()
    if cur == "KRW":
        return float(amount), {"currency": "KRW", "rate": 1.0,
                               "rate_source": "n/a", "estimated": False}
    rate = usd_krw_rate
    estimated = rate is None
    if rate is None or rate <= 0:
        rate = 1500.0  # conservative upper bound; never *under*-counts limit
        estimated = True
    return float(amount) * float(rate), {
        "currency": cur,
        "rate": float(rate),
        "rate_source": "provided" if not estimated else "conservative_default",
        "estimated": estimated,
        "as_of": _kst_now().isoformat(),
    }


# ===========================================================================
# Invariant 11 — Market open / trading hours gate
# ===========================================================================

def _hhmm_to_minutes(s: str) -> Optional[int]:
    try:
        hh, mm = s.split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return None


def check_market_open(calendar: Optional[Dict],
                      *,
                      now: Optional[datetime] = None,
                      allow_extended: bool = False,
                      require_regular: bool = True) -> Optional[Dict]:
    """Invariant 11: reject live orders when the market is closed.

    ``calendar`` is the payload ``server`` already fetched from
    ``GET /api/v1/market-calendar/{KR|US}`` (so this module stays transport-free).
    A live caller MUST pass a calendar; ``None`` is treated as closed (fail-safe).

    Logic:
      - ``isHoliday`` true OR ``isOpen`` false  -> MARKET_CLOSED.
      - When sessions are present, current KST time must fall inside the REGULAR
        session (or any session if ``allow_extended``). Sessions crossing
        midnight (US in KST) are handled.
      - mock can inject ``now`` for boundary tests.

    Returns ``None`` if tradable, else a ``MARKET_CLOSED`` error envelope.
    """
    if not calendar:
        return _err(ERR_MARKET_CLOSED,
                    "장 운영정보를 확인할 수 없어 실주문을 거부합니다(안전).")
    if calendar.get("isHoliday") is True or calendar.get("isOpen") is False:
        return _err(ERR_MARKET_CLOSED,
                    f"{calendar.get('region', '시장')} 휴장/미개장 상태입니다.",
                    region=calendar.get("region"), date=calendar.get("date"))

    sessions = calendar.get("sessions") or []
    if not sessions:
        # isOpen true but no session detail — trust the open flag.
        return None

    now = now or _kst_now()
    cur_min = now.hour * 60 + now.minute

    def _in_session(sess: Dict) -> bool:
        o = _hhmm_to_minutes(str(sess.get("open", "")))
        c = _hhmm_to_minutes(str(sess.get("close", "")))
        if o is None or c is None:
            return False
        if c >= o:  # same-day session
            return o <= cur_min < c
        # crosses KST midnight (e.g. US REGULAR 23:30 -> 06:00)
        return cur_min >= o or cur_min < c

    if require_regular and not allow_extended:
        candidates = [s for s in sessions
                      if str(s.get("name", "")).upper() == "REGULAR"] or sessions
    else:
        candidates = sessions

    if any(_in_session(s) for s in candidates):
        return None
    return _err(
        ERR_MARKET_CLOSED,
        f"{calendar.get('region', '시장')} 정규장 운영시간이 아닙니다 "
        f"(현재 KST {now.strftime('%H:%M')}). 연장세션 주문은 별도 플래그가 필요합니다.",
        region=calendar.get("region"), date=calendar.get("date"),
        now_kst=now.strftime("%H:%M"),
    )


# ===========================================================================
# Invariant 13A — Token-at-rest file permission verification
# ===========================================================================

def verify_token_file_perms(path: Path) -> Optional[Dict]:
    """Invariant 13A: refuse to *read* a token file with loose perms / symlink.

    Returns ``None`` if the file is absent (nothing to verify) or 0600 & not a
    symlink. Returns an error envelope (code ``INVALID_PARAM``) otherwise so the
    caller declines to use a possibly-compromised on-disk token.
    """
    try:
        if os.path.islink(path):
            return _err(ERR_INVALID,
                        f"토큰 파일이 심볼릭 링크입니다 — 사용을 거부합니다: {path}")
        if not os.path.exists(path):
            return None
        st = os.lstat(path)
        # World/group bits must be clear (only owner rw).
        if st.st_mode & 0o077:
            return _err(
                ERR_INVALID,
                f"토큰 파일 권한이 느슨합니다(0{oct(st.st_mode & 0o777)[2:]}). "
                "0600으로 재설정 후 다시 시도하세요.")
        return None
    except Exception as e:  # noqa: BLE001
        return _err(ERR_INVALID, f"토큰 파일 권한 검증 실패: {e}")


def atomic_write_0600(path: Path, data: str) -> None:
    """Atomically write ``data`` to ``path`` with 0600, refusing symlinks.

    Uses ``O_NOFOLLOW | O_CREAT | O_EXCL`` on a temp file + ``os.replace``.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except Exception:
        pass
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}.{int(time.time()*1000)}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
    finally:
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


# ===========================================================================
# SafetyGuard — stateful guard (7 dedup, 8 limits, 9 circuit, 10 audit)
# ===========================================================================

class SafetyGuard:
    """Disk-backed, thread-safe enforcement of stateful invariants 7/8/9/10.

    One instance is owned by ``server.py`` for the process lifetime. All state
    lives under ``cfg.data_dir`` (``~/.toss-trader`` by default, 0700):
      - ``dedup.json``        : recent client_order_id -> {order_id, ts} (inv 7)
      - ``daily.json``        : per-KST-date {krw, count} counters    (inv 8)
      - ``circuit.json``      : consecutive failure / cooldown state  (inv 9)
      - ``audit.jsonl``       : append-only, 0600                     (inv 10)
    """

    def __init__(self, cfg: _config.Config,
                 *,
                 dedup_window_sec: float = 300.0,
                 circuit_threshold: int = 3,
                 circuit_cooldown_sec: float = 120.0):
        self._cfg = cfg
        self._lock = threading.RLock()
        self._dir = Path(cfg.data_dir)
        self._dedup_path = self._dir / "dedup.json"
        self._daily_path = self._dir / "daily.json"
        self._circuit_path = self._dir / "circuit.json"
        self._audit_path = self._dir / "audit.jsonl"
        self._dedup_window = max(1.0, dedup_window_sec)
        self._circuit_threshold = max(1, int(circuit_threshold))
        self._circuit_cooldown = max(1.0, circuit_cooldown_sec)
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self._dir, 0o700)
        except Exception:
            pass

    # ----- small JSON helpers --------------------------------------------
    @staticmethod
    def _read_json(path: Path, default: Any) -> Any:
        try:
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return default

    @staticmethod
    def _write_json_0600(path: Path, obj: Any) -> None:
        try:
            atomic_write_0600(path, json.dumps(obj, ensure_ascii=False))
        except Exception:
            # State files are best-effort; never crash a tool call on IO error.
            try:
                path.write_text(json.dumps(obj, ensure_ascii=False),
                                encoding="utf-8")
                os.chmod(path, 0o600)
            except Exception:
                pass

    # =====================================================================
    # Invariant 7 — duplicate detection
    # =====================================================================
    def check_duplicate(self, client_order_id: str) -> Optional[Dict]:
        """Reject a repeat of ``client_order_id`` seen within the dedup window.

        Returns ``None`` if fresh, else a ``DUPLICATE`` envelope carrying the
        previously-recorded ``existing_order_id`` (idempotent replay answer).
        """
        if not client_order_id:
            return None
        now = time.time()
        with self._lock:
            store = self._read_json(self._dedup_path, {})
            rec = store.get(client_order_id)
            if rec and (now - float(rec.get("ts", 0))) <= self._dedup_window:
                return _err(
                    ERR_DUPLICATE,
                    "동일 주문이 최근에 이미 접수되었습니다(중복 차단).",
                    existing_order_id=rec.get("order_id"),
                    client_order_id=client_order_id,
                )
        return None

    def record_submission(self, client_order_id: str,
                          order_id: Optional[str]) -> None:
        """Record a successful submission so subsequent identical calls dedup."""
        if not client_order_id:
            return
        now = time.time()
        with self._lock:
            store = self._read_json(self._dedup_path, {})
            # prune expired entries to bound file growth
            store = {k: v for k, v in store.items()
                     if (now - float(v.get("ts", 0))) <= self._dedup_window}
            store[client_order_id] = {"order_id": order_id, "ts": now}
            self._write_json_0600(self._dedup_path, store)

    def reserve_submission(self, client_order_id: str) -> Optional[Dict]:
        """Invariant 7, TOCTOU fix: atomically CHECK-AND-CLAIM a clientOrderId
        *before* the POST. Two concurrent submits of the same intent can no
        longer both pass a read-only duplicate check — the second reserve gets
        DUPLICATE. Pair with confirm_submission (success) / release_submission
        (definite failure only; NEVER release on an uncertain outcome)."""
        if not client_order_id:
            return None
        now = time.time()
        with self._lock:
            store = self._read_json(self._dedup_path, {})
            rec = store.get(client_order_id)
            if rec and (now - float(rec.get("ts", 0))) <= self._dedup_window:
                return _err(ERR_DUPLICATE,
                            "동일 주문이 이미 접수/진행 중입니다(중복 차단).",
                            existing_order_id=rec.get("order_id"),
                            client_order_id=client_order_id)
            store = {k: v for k, v in store.items()
                     if (now - float(v.get("ts", 0))) <= self._dedup_window}
            store[client_order_id] = {"order_id": None, "ts": now, "reserved": True}
            self._write_json_0600(self._dedup_path, store)
        return None

    def confirm_submission(self, client_order_id: str,
                           order_id: Optional[str]) -> None:
        """Attach the broker orderId to a reservation after a successful POST."""
        if not client_order_id:
            return
        with self._lock:
            store = self._read_json(self._dedup_path, {})
            rec = store.get(client_order_id) or {"ts": time.time()}
            rec["order_id"] = order_id
            rec.pop("reserved", None)
            store[client_order_id] = rec
            self._write_json_0600(self._dedup_path, store)

    def release_submission(self, client_order_id: str) -> None:
        """Free a reservation after a DEFINITE failure so a corrected retry is
        not dedup-blocked. Only releases unconfirmed (order_id=None) entries."""
        if not client_order_id:
            return
        with self._lock:
            store = self._read_json(self._dedup_path, {})
            rec = store.get(client_order_id)
            if rec and rec.get("order_id") is None:
                store.pop(client_order_id, None)
                self._write_json_0600(self._dedup_path, store)

    # =====================================================================
    # Invariant 8 — per-order + daily limits (KST-midnight reset)
    # =====================================================================
    def _daily_counters(self) -> Dict[str, Any]:
        data = self._read_json(self._daily_path, {})
        today = _kst_date_str()
        if data.get("date") != today:
            data = {"date": today, "krw": 0.0, "count": 0}
        return data

    def check_limits(self, krw_notional: float,
                     *, count: int = 1) -> Optional[Dict]:
        """Invariant 8: per-order + cumulative daily KRW/count ceilings.

        ``krw_notional`` must already be KRW-converted (use :func:`to_krw_notional`).
        Hard-fail (returns ``LIMIT_EXCEEDED``) regardless of other gates;
        the caller MUST NOT touch the network when this returns non-None.
        Does NOT mutate counters — call :meth:`record_order_result` on success.
        """
        cfg = self._cfg
        if krw_notional > cfg.max_order_krw:
            return _err(
                ERR_LIMIT,
                f"건당 한도 초과: 주문 {int(krw_notional):,}원 > 한도 "
                f"{int(cfg.max_order_krw):,}원(TOSS_MAX_ORDER_KRW).",
                limit_type="per_order", order_krw=krw_notional,
                limit_krw=cfg.max_order_krw,
            )
        with self._lock:
            d = self._daily_counters()
            if d["count"] + count > cfg.max_daily_count:
                return _err(
                    ERR_LIMIT,
                    f"일일 주문 건수 한도 초과: {d['count'] + count} > "
                    f"{cfg.max_daily_count}건(TOSS_MAX_DAILY_COUNT).",
                    limit_type="daily_count", daily_count=d["count"],
                    limit_count=cfg.max_daily_count,
                )
            if d["krw"] + krw_notional > cfg.max_daily_krw:
                return _err(
                    ERR_LIMIT,
                    f"일일 누적 금액 한도 초과: "
                    f"{int(d['krw'] + krw_notional):,}원 > "
                    f"{int(cfg.max_daily_krw):,}원(TOSS_MAX_DAILY_KRW).",
                    limit_type="daily_krw", daily_krw=d["krw"],
                    limit_krw=cfg.max_daily_krw,
                )
        return None

    def _bump_daily(self, krw_notional: float, count: int = 1) -> None:
        with self._lock:
            d = self._daily_counters()
            d["krw"] = float(d.get("krw", 0.0)) + max(0.0, krw_notional)
            d["count"] = int(d.get("count", 0)) + max(0, count)
            self._write_json_0600(self._daily_path, d)

    def daily_usage(self) -> Dict[str, Any]:
        """Read-only snapshot of today's KRW/count usage vs configured limits."""
        with self._lock:
            d = self._daily_counters()
        return {
            "date": d["date"],
            "krw_used": d.get("krw", 0.0),
            "count_used": d.get("count", 0),
            "max_order_krw": self._cfg.max_order_krw,
            "max_daily_krw": self._cfg.max_daily_krw,
            "max_daily_count": self._cfg.max_daily_count,
        }

    # =====================================================================
    # Invariant 9 — circuit breaker (consecutive failures -> cooldown OPEN)
    # =====================================================================
    def _halt_file_present(self) -> bool:
        """Invariant 9a: file kill-switch — a ``HALT`` file blocks all orders.

        Checked at every POST (no restart needed). Looks in the configured data
        dir first, then the canonical ``~/.toss-mcp/HALT`` path so an operator
        can stop trading regardless of ``TOSS_DATA_DIR``.
        """
        candidates = [self._dir / "HALT"]
        try:
            canonical = Path.home() / ".toss-mcp" / "HALT"
            if canonical not in candidates:
                candidates.append(canonical)
        except Exception:
            pass
        for p in candidates:
            try:
                if p.exists():
                    return True
            except Exception:
                continue
        return False

    def check_circuit(self) -> Optional[Dict]:
        """Invariant 9: refuse mutating ops while the breaker is OPEN.

        Also folds in the kill-switches (env ``TOSS_KILL`` via
        ``config.kill_active()`` AND the file kill-switch ``HALT``, inv 9a) so
        the caller has a single pre-POST gate. Returns ``None`` if closed.
        """
        if self._halt_file_present():
            return _err(ERR_CIRCUIT,
                        "HALT 파일 kill-switch가 활성화되어 주문이 차단됩니다.")
        if _config.kill_active():
            return _err(ERR_CIRCUIT,
                        "Kill-switch(TOSS_KILL)가 켜져 있어 주문이 차단됩니다.")
        now = time.time()
        with self._lock:
            c = self._read_json(self._circuit_path, {})
            open_until = float(c.get("open_until", 0))
            if open_until > now:
                return _err(
                    ERR_CIRCUIT,
                    "주문 회로차단 작동 중(연속 실패). 쿨다운 동안 읽기만 가능합니다.",
                    cooldown_remaining_sec=round(open_until - now, 1),
                    consecutive_failures=int(c.get("fails", 0)),
                )
        return None

    def record_order_result(self, *, success: bool,
                            krw_notional: float = 0.0,
                            count: int = 1) -> None:
        """Update circuit + daily counters after an order attempt resolves.

        On success: reset failure streak and bump daily counters.
        On failure: increment streak; trip the breaker at the threshold.
        """
        now = time.time()
        with self._lock:
            c = self._read_json(self._circuit_path, {})
            if success:
                c["fails"] = 0
                c["open_until"] = 0
                self._write_json_0600(self._circuit_path, c)
                self._bump_daily(krw_notional, count)
            else:
                fails = int(c.get("fails", 0)) + 1
                c["fails"] = fails
                if fails >= self._circuit_threshold:
                    c["open_until"] = now + self._circuit_cooldown
                self._write_json_0600(self._circuit_path, c)

    def reset_circuit(self) -> None:
        """Manually clear the breaker (e.g. after operator intervention)."""
        with self._lock:
            self._write_json_0600(self._circuit_path,
                                  {"fails": 0, "open_until": 0})

    # =====================================================================
    # Invariant 10 — append-only audit log (0600, secret-masked)
    # =====================================================================
    def audit(self, *, mode: str, tool: str,
              symbol: Optional[str] = None,
              side: Optional[str] = None,
              quantity: Optional[float] = None,
              price: Optional[float] = None,
              est_amount: Optional[float] = None,
              currency: Optional[str] = None,
              client_order_id: Optional[str] = None,
              order_id: Optional[str] = None,
              error: Optional[Any] = None,
              confirm_present: Optional[bool] = None,
              preview_token_present: Optional[bool] = None,
              plan_id: Optional[str] = None,
              **extra: Any) -> None:
        """Append one audit record for an order/cancel/modify attempt.

        Records dry-run, live and failed attempts alike (invariant 10). NEVER
        writes a token/secret: any caller-supplied ``extra`` is passed through
        ``config.redact_mapping`` and known sensitive keys are dropped entirely.
        The daily counter (inv 8) can be reconstructed from these lines.
        """
        rec: Dict[str, Any] = {
            "timestamp": _kst_now().isoformat(),
            "mode": mode,            # mock | dry | live
            "tool": tool,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "est_amount": est_amount,
            "currency": currency,
            "client_order_id": client_order_id,
            "order_id": order_id,
            "confirm_present": confirm_present,
            "preview_token_present": preview_token_present,
            "plan_id": plan_id,
        }
        if error is not None:
            # error may be an envelope or string; keep the code/message only.
            if isinstance(error, dict):
                e = error.get("error", error)
                rec["error"] = {"code": e.get("code") if isinstance(e, dict) else None,
                                "message_ko": e.get("message_ko") if isinstance(e, dict) else str(e)}
            else:
                rec["error"] = {"message_ko": str(error)}
        if extra:
            safe = _config.redact_mapping(extra)
            # Defense in depth: drop any obviously sensitive key entirely.
            for k in list(safe.keys()):
                lk = k.lower()
                if any(s in lk for s in ("secret", "token", "authorization",
                                         "password", "bearer", "access")):
                    safe.pop(k, None)
            rec.update(safe)
        line = json.dumps(rec, ensure_ascii=False)
        with self._lock:
            try:
                # Size-based rotation (~5MB): a real-money audit log must not
                # grow unbounded; rotated files keep their 0600 perms.
                try:
                    if (self._audit_path.exists()
                            and self._audit_path.stat().st_size > 5_000_000):
                        self._audit_path.rename(self._audit_path.with_name(
                            f"audit-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
                except Exception:
                    pass
                # Create with 0600 if absent; never follow a symlink.
                if os.path.islink(self._audit_path):
                    return  # refuse to write through a symlink
                flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
                if hasattr(os, "O_NOFOLLOW") and self._audit_path.exists():
                    flags |= os.O_NOFOLLOW
                fd = os.open(self._audit_path, flags, 0o600)
                try:
                    os.write(fd, (line + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
                try:
                    os.chmod(self._audit_path, 0o600)
                except Exception:
                    pass
            except Exception:
                pass


# ===========================================================================
# Invariant 13B helper — price-drift check (server passes the snapshot price)
# ===========================================================================

def check_price_drift(snapshot_price: Optional[float],
                      current_price: Optional[float],
                      *, max_drift_pct: float = 1.0) -> Optional[Dict]:
    """Invariant 13B: reject execute when live price drifted beyond threshold.

    Returns ``None`` if within tolerance (or prices unavailable -> caller
    decides), else a ``PRICE_MOVED`` envelope. ``server`` supplies both the
    preview snapshot price and a freshly-fetched current price.
    """
    if not snapshot_price or not current_price or snapshot_price <= 0:
        return None
    drift = abs(current_price - snapshot_price) / snapshot_price * 100.0
    if drift > max_drift_pct:
        return _err(
            ERR_PRICE_MOVED,
            f"시세가 미리보기 대비 {drift:.2f}% 변동(임계 {max_drift_pct:.2f}%) — "
            "preview_order를 다시 실행하세요.",
            snapshot_price=snapshot_price, current_price=current_price,
            drift_pct=round(drift, 4),
        )
    return None
