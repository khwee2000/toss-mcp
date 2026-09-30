"""toss_client.py — Toss Open API transport wrapper.

Responsibilities:
- OAuth2 ``client_credentials`` token acquisition + caching (memory + 0600
  ``token.json``), proactive refresh, single-flight lock.
- Thin HTTP layer over ``urllib`` (no third-party deps) with rate-limit header
  awareness, ``Retry-After`` honouring, and exponential backoff on 429/5xx.
- A single ``TossClient`` facade whose methods every MCP tool calls. The client
  routes to either the network (live) or :mod:`mock_data` (mock) based on the
  resolved :class:`config.Config` — so tool code has zero mode branches.
- A ``Reconciler`` helper: after an order POST whose response is uncertain
  (timeout / 5xx), it GETs the order rather than ever re-POSTing.

SECURITY
--------
- ``client_secret`` / ``access_token`` are NEVER logged. All logging goes
  through :class:`_RedactingFilter`.
- In mock mode the HTTP layer is hard-disabled (``_http`` raises), guaranteeing
  no socket is opened while the user has no key.

Pure stdlib so it imports under any Python 3.9+ without ``mcp`` installed.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from config import Config, kill_active, redact
from mock_data import MockData, get_mock

# ---------------------------------------------------------------------------
# Logging with secret redaction
# ---------------------------------------------------------------------------

_SECRET_PATTERNS = ("Bearer ", "client_secret", "access_token", "Authorization")


class _RedactingFilter(logging.Filter):
    """Mask token/secret material in log records (regex-based, in-place).

    Unlike the old whole-message replacement, this masks ONLY the credential
    substrings so the rest of the log line stays useful, and it can't be
    bypassed by a token arriving via %-args (args are folded first)."""

    _PATTERNS = [
        re.compile(r"eyJ[A-Za-z0-9_\-]{8,}(?:\.[A-Za-z0-9_\-\.]+)?"),   # JWT
        re.compile(r"ts[sc]k_[A-Za-z0-9_]{6,}"),                        # tssk_/tsck_ keys
        re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.]{8,}"),             # Authorization
        re.compile(r"(?i)(client_secret[=:]\s*)[^&\s\"']{4,}"),         # form/query/json
        re.compile(r"(?i)(access_token[\"']?\s*[=:]\s*[\"']?)[^&\s\"']{8,}"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
        except Exception:
            return True
        red = msg
        for pat in self._PATTERNS:
            red = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "<redacted>", red)
        if red != msg:
            record.msg = red
            record.args = ()
        return True


def _make_logger() -> logging.Logger:
    log = logging.getLogger("toss_trader")
    if not log.handlers:
        h = logging.StreamHandler()  # stderr — never stdout (stdout is MCP stdio)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        h.addFilter(_RedactingFilter())
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log


LOG = _make_logger()


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


# Official Toss error ``code`` (openapi.json v1.1.1) -> our canonical/standard
# code. Keeps a single, doc-grounded mapping so server envelopes stay consistent.
# - request/param errors -> INVALID_PARAM
# - OAuth/client errors  -> token/auth-error (never leaks the secret)
# - account-header-required is a *config* error (we forgot the account header).
ERROR_CODE_MAP = {
    "invalid-request": "INVALID_PARAM",
    "stock-not-found": "INVALID_PARAM",
    "exchange-rate-not-found": "INVALID_PARAM",
    "unsupported-date": "INVALID_PARAM",
    "account-header-required": "config-error",
    "invalid_client": "auth-error",
    "unsupported_grant_type": "auth-error",
}


class TossApiError(Exception):
    """Carries a flat string ``code`` plus optional ``data`` hints.

    ``request_id`` preserves the live ``error.requestId`` (when the BFF supplies
    it) so a failed call can be correlated with Toss support logs. It is never a
    secret, so it is safe to surface in the error envelope.
    """

    def __init__(self, code: str, message: str = "", data: Optional[Dict] = None,
                 status: Optional[int] = None, request_id: Optional[str] = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data or {}
        self.status = status
        self.request_id = request_id

    def to_dict(self) -> Dict:
        err = {"code": self.code, "message": self.message,
               "data": self.data, "status": self.status}
        if self.request_id:
            err["requestId"] = self.request_id
        return {"error": err}


class TossNetworkUncertain(Exception):
    """Raised when an order POST's outcome is unknown (timeout/5xx).

    The caller MUST reconcile via GET, never re-POST.
    """


# ---------------------------------------------------------------------------
# Token manager
# ---------------------------------------------------------------------------


class TokenManager:
    """OAuth2 client_credentials token cache with single-flight refresh."""

    def __init__(self, cfg: Config):
        self._cfg = cfg
        self._lock = threading.Lock()
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._path = cfg.data_dir / "token.json"

    def _load_disk(self) -> None:
        # Invariant 13A: token-at-rest is OPT-IN. Default is memory-only — never
        # read a cached token unless the operator set TOSS_TOKEN_PERSIST.
        if not self._cfg.token_persist:
            return
        try:
            import safety
            # Refuse a loose-perms / symlinked token file (possibly tampered).
            if safety.verify_token_file_perms(self._path) is not None:
                self._token, self._expires_at = None, 0.0
                return
            if self._path.is_file():
                d = json.loads(self._path.read_text(encoding="utf-8"))
                issued = float(d.get("issued_at", 0))
                ttl = float(d.get("expires_in", 0))
                self._token = d.get("access_token")
                self._expires_at = issued + ttl
        except Exception:
            self._token, self._expires_at = None, 0.0

    def _save_disk(self, token: str, expires_in: float) -> None:
        # Invariant 13A: only persist when explicitly opted in; write atomically
        # with 0600 + O_NOFOLLOW (no plaintext token left by default).
        if not self._cfg.token_persist:
            return
        try:
            import safety
            safety.atomic_write_0600(
                self._path,
                json.dumps(
                    {"access_token": token, "issued_at": time.time(),
                     "expires_in": expires_in}
                ),
            )
        except Exception as exc:  # pragma: no cover
            LOG.warning("token.json persist failed: %s", type(exc).__name__)

    def logout(self) -> Dict:
        """Invariant 13A: drop the in-memory token and delete any on-disk
        token.json. The next call transparently re-issues a fresh token."""
        self._token, self._expires_at = None, 0.0
        deleted = False
        try:
            if self._path.exists():
                self._path.unlink()
                deleted = True
        except Exception:  # noqa: BLE001
            pass
        return {"tokenCleared": True, "fileDeleted": deleted}

    def _valid(self) -> bool:
        # Refresh ~10% before expiry.
        return bool(self._token) and time.time() < (self._expires_at - 60)

    def get_token(self) -> str:
        if self._valid():
            return self._token  # type: ignore[return-value]
        with self._lock:
            if self._valid():
                return self._token  # type: ignore[return-value]
            self._load_disk()
            if self._valid():
                return self._token  # type: ignore[return-value]
            return self._refresh()

    def _refresh(self) -> str:
        if not (self._cfg.client_id and self._cfg.client_secret):
            raise TossApiError("no-credentials",
                               "client_id/client_secret not configured")
        url = f"{self._cfg.base_url}/oauth2/token"
        body = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": self._cfg.client_id,
                "client_secret": self._cfg.client_secret,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 401 -> fall back to HTTP Basic auth once (spec ambiguity guard).
            if e.code == 401:
                payload = self._refresh_basic(url)
            else:
                raise TossApiError("token-error", f"HTTP {e.code}", status=e.code)
        except urllib.error.URLError as e:
            raise TossApiError("network-error", str(e.reason))
        token = payload.get("access_token")
        expires_in = float(payload.get("expires_in", 3600))
        if not token:
            raise TossApiError("token-error", "no access_token in response")
        self._token = token
        self._expires_at = time.time() + expires_in
        self._save_disk(token, expires_in)
        LOG.info("OAuth token acquired (expires_in=%ss)", int(expires_in))
        return token

    def _refresh_basic(self, url: str) -> Dict:
        basic = base64.b64encode(
            f"{self._cfg.client_id}:{self._cfg.client_secret}".encode()
        ).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Rate guard
# ---------------------------------------------------------------------------


class RateGuard:
    """Per-group header-aware backoff + a GLOBAL token bucket (invariant 9c).

    The global bucket paces ALL requests regardless of group so a burst mixing
    many groups can't exceed ~1/GLOBAL_MIN_INTERVAL req/s in aggregate."""

    GLOBAL_MIN_INTERVAL = 0.1   # ≈10 req/s hard aggregate ceiling

    def __init__(self):
        self._lock = threading.Lock()
        self._next_ok: Dict[str, float] = {}
        self._global_next = 0.0

    def before(self, group: str) -> None:
        with self._lock:
            now = time.time()
            wait = max(self._next_ok.get(group, 0.0) - now,
                       self._global_next - now)
            # claim the next global slot (leaky bucket: one request per interval)
            self._global_next = max(self._global_next, now) + self.GLOBAL_MIN_INTERVAL
        if wait > 0:
            time.sleep(min(wait, 5.0))

    def observe(self, group: str, headers: Dict[str, str]) -> None:
        # Honour Retry-After / x-ratelimit-reset when remaining hits 0.
        # Toss emits LOWERCASE ``x-ratelimit-*`` headers (openapi.json v1.1.1);
        # ``dict(resp.headers)`` preserves case, so a case-sensitive ``.get``
        # silently missed them. Normalize to a lower-key dict and look up by
        # lowercase so both ``Retry-After`` and ``x-ratelimit-*`` are caught.
        low = {}
        try:
            for k, v in (headers or {}).items():
                low[str(k).lower()] = v
        except (AttributeError, TypeError):
            low = {}
        retry_after = low.get("retry-after")
        remaining = low.get("x-ratelimit-remaining")
        delay = 0.0
        if retry_after is not None:
            try:
                delay = float(str(retry_after).strip())
            except ValueError:
                delay = 1.0
        elif remaining is not None and str(remaining).strip() in {"0", "0.0"}:
            reset = low.get("x-ratelimit-reset")
            try:
                delay = max(0.0, float(str(reset).strip()) - time.time()) if reset else 1.0
            except ValueError:
                delay = 1.0
        if delay > 0:
            with self._lock:
                self._next_ok[group] = time.time() + delay


# ---------------------------------------------------------------------------
# Live -> canonical response normalization
# ---------------------------------------------------------------------------
#
# The official Toss Open API (https://openapi.tossinvest.com/openapi-docs/
# latest/openapi.json) returns fields whose names differ from the canonical
# shape that :mod:`mock_data` emits and that :mod:`analytics` / :mod:`indicators`
# consume. mock_data IS the canonical contract; we normalize *live* result
# payloads into that exact shape so every analytics/indicator path is mode-blind.
#
# Confirmed live field maps (result is already envelope-unwrapped upstream):
#   candles  : {candles:[{openPrice,highPrice,lowPrice,closePrice,volume,
#                         timestamp,currency}], nextBefore}
#              -> {symbol, interval, candles:[{open,high,low,close,volume,
#                         timestamp,currency}], nextBefore}
#   prices   : [{symbol, timestamp, lastPrice, currency}]   (a LIST)
#              -> {prices:[{symbol, timestamp, lastPrice, currency}]}
#   orderbook: {timestamp, currency, asks:[{price,volume}], bids:[{price,volume}]}
#              -> {symbol, timestamp, currency, asks/bids:[{price,quantity}], ...}
#   trades   : [{price, volume, timestamp, currency}]   (LIST, NO side/quantity)
#              -> {symbol, trades:[{price, quantity, side(inferred), timestamp,
#                         currency, sideInferred:true}]}
#   accounts : [{accountNo, accountSeq, accountType}]   (a LIST)
#              -> {accounts:[...]}
#   holdings : {totalPurchaseAmount, marketValue, profitLoss, dailyProfitLoss,
#                         items:[...]}  -> {holdings:[...rows...], summary:{...}}
#   price-limits/exchange-rate/market-calendar/orders : light field bridging.
#
# Numeric strings ("37.21") are kept as strings here (indicators/analytics call
# float() themselves); only structural/key changes happen in this layer.

class _Normalize:
    """Pure live-result -> canonical-shape transforms (stateless, no network)."""

    # ----- market data -------------------------------------------------
    @staticmethod
    def candles(result, symbol: str, interval: str) -> Dict:
        """Live candles result -> mock-canonical candles envelope.

        Maps openPrice/highPrice/lowPrice/closePrice -> open/high/low/close so
        :func:`indicators._ohlcv` (which reads c['close'] etc.) works unchanged.
        """
        if isinstance(result, dict):
            raw = result.get("candles", []) or []
            next_before = result.get("nextBefore")
        else:  # tolerate a bare list
            raw, next_before = (result or []), None
        out = []
        for c in raw:
            if not isinstance(c, dict):
                continue
            row = dict(c)  # keep currency/timestamp and any extras verbatim
            # Map *Price -> short OHLC keys only when the canonical key is absent.
            row.setdefault("open", c.get("openPrice", c.get("open")))
            row.setdefault("high", c.get("highPrice", c.get("high")))
            row.setdefault("low", c.get("lowPrice", c.get("low")))
            row.setdefault("close", c.get("closePrice", c.get("close")))
            row.setdefault("volume", c.get("volume"))
            # Drop the now-aliased long names to keep the row clean/canonical.
            for k in ("openPrice", "highPrice", "lowPrice", "closePrice"):
                row.pop(k, None)
            out.append(row)
        # CRITICAL: the live feed returns candles NEWEST-FIRST, but indicators/
        # analytics (SMA/RSI/change/disparity/scalpScore) assume OLDEST-FIRST
        # chronological order — exactly like the mock canonical shape. Without
        # this sort, live analyze_symbol/scalp_score compute garbage (e.g. MA20
        # from the oldest bars, change% off a stale reference). Sort ascending by
        # timestamp so the canonical contract is always chronological.
        out.sort(key=lambda r: str(r.get("timestamp") or ""))
        return {"symbol": symbol, "interval": interval,
                "candles": out, "nextBefore": next_before, "_mode": "live"}

    @staticmethod
    def _one_price(p: Dict) -> Dict:
        """A live price element -> canonical single-price row (best-effort)."""
        row = dict(p) if isinstance(p, dict) else {}
        row.setdefault("lastPrice", p.get("lastPrice") if isinstance(p, dict) else None)
        return row

    @staticmethod
    def prices(result) -> Dict:
        """Live prices result (a LIST) -> {prices:[...]} canonical envelope."""
        if isinstance(result, dict) and "prices" in result:
            rows = result.get("prices", []) or []
        elif isinstance(result, list):
            rows = result
        else:
            rows = [result] if result else []
        return {"prices": [_Normalize._one_price(p) for p in rows],
                "_mode": "live"}

    @staticmethod
    def price(result, symbol: str) -> Dict:
        """Single price: live returns an array even for one symbol -> first row."""
        env = _Normalize.prices(result)
        rows = env.get("prices", [])
        # Prefer the exact symbol match, else the first row.
        chosen = next((r for r in rows if r.get("symbol") == symbol), None)
        chosen = chosen or (rows[0] if rows else {})
        out = dict(chosen)
        out.setdefault("symbol", symbol)
        out["_mode"] = "live"
        return out

    @staticmethod
    def _level(lvl: Dict) -> Dict:
        """Orderbook level: live uses {price,volume}; canonical uses quantity."""
        if not isinstance(lvl, dict):
            return {"price": None, "quantity": None}
        return {"price": lvl.get("price"),
                "quantity": lvl.get("quantity", lvl.get("volume"))}

    @staticmethod
    def orderbook(result, symbol: str) -> Dict:
        r = result if isinstance(result, dict) else {}
        bids = [_Normalize._level(l) for l in (r.get("bids") or [])]
        asks = [_Normalize._level(l) for l in (r.get("asks") or [])]

        def _qty(levels):
            tot = 0.0
            for l in levels:
                try:
                    tot += float(l.get("quantity") or 0)
                except (TypeError, ValueError):
                    pass
            return tot

        out = {
            "symbol": symbol,
            "timestamp": r.get("timestamp"),
            "currency": r.get("currency"),
            "bids": bids, "asks": asks,
            "totalBidQuantity": _qty(bids),
            "totalAskQuantity": _qty(asks),
            "_mode": "live",
        }
        if bids:
            out["bestBid"] = bids[0].get("price")
        if asks:
            out["bestAsk"] = asks[0].get("price")
        return out

    @staticmethod
    def trades(result, symbol: str) -> Dict:
        """Live trades (LIST, no side, volume-not-quantity) -> canonical tape.

        Live `/trades` has neither a per-print side nor a `quantity` key — only
        `volume`. 체결강도(buy/sell ratio) and tick-flow need a side, so we infer
        it with the classic *tick rule* and stamp ``sideInferred: true`` on every
        row plus ``_sideInferred: true`` on the envelope. We NEVER present an
        inferred side as a Toss-provided fact.

        Tick rule (applied oldest->newest): uptick=BUY, downtick=SELL, equal
        price keeps the previous classification (first tick defaults to BUY).
        Live result is newest-first, so we walk a reversed copy to assign, then
        restore newest-first ordering for the canonical envelope.
        """
        rows = result if isinstance(result, list) else (
            result.get("trades", []) if isinstance(result, dict) else [])
        rows = list(rows or [])
        # Walk oldest->newest for the tick rule (Toss tape is newest-first).
        ordered = list(reversed(rows))
        prev_px = None
        last_side = "BUY"
        classified = []
        for t in ordered:
            if not isinstance(t, dict):
                continue
            row = dict(t)
            # Canonical quantity := live volume (live has no `quantity`).
            row.setdefault("quantity", t.get("quantity", t.get("volume")))
            try:
                px = float(t.get("price")) if t.get("price") is not None else None
            except (TypeError, ValueError):
                px = None
            if px is not None and prev_px is not None:
                if px > prev_px:
                    last_side = "BUY"
                elif px < prev_px:
                    last_side = "SELL"
                # equal price -> keep last_side
            row["side"] = last_side
            row["sideInferred"] = True
            if px is not None:
                prev_px = px
            classified.append(row)
        # Restore newest-first to match the canonical (mock) tape ordering.
        classified.reverse()
        return {"symbol": symbol, "trades": classified,
                "_sideInferred": True, "_mode": "live"}

    @staticmethod
    def stocks(result) -> Dict:
        """Live ``GET /api/v1/stocks`` result (an ARRAY of stock masters) ->
        canonical ``{stocks:[...]}`` envelope.

        Each live item (openapi.json v1.1.1):
          {symbol, name, englishName, isinCode, market, securityType,
           isCommonShare(bool), status, currency, listDate, delistDate|null,
           sharesOutstanding(str), leverageFactor(str|null),
           koreanMarketDetail:{liquidationTrading, nxtSupported,
           krxTradingSuspended, nxtTradingSuspended}|null}

        Numeric strings (sharesOutstanding/leverageFactor) are KEPT as strings
        (callers float() them). We only wrap the array into a dict envelope and
        keep every field verbatim so the analysis layer is mode-blind.
        """
        if isinstance(result, dict) and "stocks" in result:
            rows = result.get("stocks", []) or []
        elif isinstance(result, list):
            rows = result
        elif isinstance(result, dict):
            rows = [result] if result.get("symbol") else []
        else:
            rows = []
        return {"stocks": [dict(r) for r in rows if isinstance(r, dict)],
                "_mode": "live"}

    @staticmethod
    def warnings(result, symbol: str) -> Dict:
        """Live ``GET /api/v1/stocks/{symbol}/warnings`` returns an ARRAY of
        warning rows (each with a live ``warningType`` field; mock uses
        ``type``) -> canonical ``{symbol, warnings:[...]}`` envelope.

        Wrapping the bare list is what lets ``classify_symbol_restrictions``
        (which already reads ``warningType``) BLOCK 정리매매/투자위험/거래정지
        in live, and stops ``.get("warnings")`` from crashing the dry-run plan,
        the execute gate, and ``analyze_symbol``.
        """
        if isinstance(result, list):
            rows = result
        elif isinstance(result, dict):
            rows = result.get("warnings", []) or []
        else:
            rows = []
        return {"symbol": symbol,
                "warnings": [dict(r) for r in rows if isinstance(r, dict)],
                "_mode": "live"}

    @staticmethod
    def commissions(result) -> Dict:
        """Live ``GET /api/v1/commissions`` returns an ARRAY of
        ``{marketCountry, commissionRate, startDate, endDate}`` -> canonical
        ``{policies:[...]}`` envelope. ``marketCountry`` is aliased to ``market``
        so it lines up with the mock policy shape; every live field is kept.
        """
        if isinstance(result, list):
            rows = result
        elif isinstance(result, dict):
            rows = result.get("policies", []) or []
        else:
            rows = []
        policies = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            p = dict(r)
            p.setdefault("market", r.get("marketCountry"))
            policies.append(p)
        return {"policies": policies, "_mode": "live"}

    @staticmethod
    def buying_power(result, currency: str) -> Dict:
        """Live ``GET /api/v1/buying-power`` returns ``{currency,
        cashBuyingPower}``; the canonical (mock) shape uses ``availableAmount``
        (+ ``withdrawableAmount``). Alias so every consumer that reads
        ``availableAmount`` works in live too (else live buys read cash=0).
        Values stay strings (callers ``float()`` them)."""
        r = result if isinstance(result, dict) else {}
        out = dict(r)
        cash = r.get("availableAmount", r.get("cashBuyingPower"))
        out.setdefault("availableAmount", cash if cash is not None else "0")
        out.setdefault("withdrawableAmount",
                       r.get("withdrawableAmount", out["availableAmount"]))
        out.setdefault("currency", r.get("currency", currency))
        out["_mode"] = "live"
        return out

    @staticmethod
    def sellable_quantity(result, symbol: str) -> Dict:
        """Live ``GET /api/v1/sellable-quantity`` returns ``sellableQuantity``
        as a STRING ("0"); mock returns a number. Coerce to a number so the
        SELL-side ``quantity > sellable`` comparison can't raise TypeError."""
        r = result if isinstance(result, dict) else {}
        out = dict(r)
        out.setdefault("symbol", symbol)
        try:
            f = float(r.get("sellableQuantity", 0) or 0)
            out["sellableQuantity"] = int(f) if f == int(f) else f
        except (TypeError, ValueError):
            out["sellableQuantity"] = 0
        out["_mode"] = "live"
        return out

    @staticmethod
    def price_limits(result, symbol: str) -> Dict:
        r = result if isinstance(result, dict) else {}
        out = dict(r)
        out["symbol"] = symbol
        out.setdefault("upperLimitPrice", r.get("upperLimitPrice"))
        out.setdefault("lowerLimitPrice", r.get("lowerLimitPrice"))
        out["_mode"] = "live"
        return out

    # ----- reference ---------------------------------------------------
    @staticmethod
    def exchange_rate(result, base: str, quote: str) -> Dict:
        r = result if isinstance(result, dict) else {}
        out = dict(r)
        out.setdefault("baseCurrency", r.get("baseCurrency", base))
        out.setdefault("quoteCurrency", r.get("quoteCurrency", quote))
        out.setdefault("rate", r.get("rate"))
        # Canonical validUntil already matches the live field name.
        out["_mode"] = "live"
        return out

    @staticmethod
    def market_calendar(result, region: str) -> Dict:
        """Bridge the nested live calendar to the flat canonical shape.

        Live: {today:{date, integrated:{preMarket,regularMarket,afterMarket}},
               previousBusinessDay, nextBusinessDay}, each session
               {startTime, singlePriceAuctionStartTime, endTime}|null.
        Canonical (mock): {region, date, isOpen, isHoliday, sessions:[...]}.
        We preserve the raw live tree under ``raw`` for callers that want it.
        """
        r = result if isinstance(result, dict) else {}
        today = r.get("today") or {}
        integrated = today.get("integrated") or {}
        sessions = []
        for name, key in (("PRE", "preMarket"), ("REGULAR", "regularMarket"),
                          ("AFTER", "afterMarket")):
            s = integrated.get(key)
            if isinstance(s, dict):
                sessions.append({
                    "name": name,
                    "open": s.get("startTime"),
                    "close": s.get("endTime"),
                    "singlePriceAuctionStartTime": s.get("singlePriceAuctionStartTime"),
                })
        is_open = bool(sessions)
        return {
            "region": region.upper(),
            "date": today.get("date"),
            "isOpen": is_open,
            "isHoliday": not is_open,
            "sessions": sessions,
            "previousBusinessDay": r.get("previousBusinessDay"),
            "nextBusinessDay": r.get("nextBusinessDay"),
            "raw": r,
            "_mode": "live",
        }

    # ----- account -----------------------------------------------------
    @staticmethod
    def accounts(result) -> Dict:
        """Live accounts is a LIST -> {accounts:[...]} canonical envelope."""
        if isinstance(result, list):
            rows = result
        elif isinstance(result, dict):
            rows = result.get("accounts", []) or []
        else:
            rows = []
        return {"accounts": list(rows), "_mode": "live"}

    @staticmethod
    def holdings(result) -> Dict:
        """Live holdings (nested totals + items) -> {holdings:[...], summary:{...}}.

        Live items carry per-symbol fields under ``items``; the canonical rows
        consumed by analytics need {symbol, currency, evaluationAmount,
        profitLoss, profitLossRate, ...}. We map best-effort and keep unknown
        keys verbatim, then build a KRW summary from the nested live totals.
        """
        r = result if isinstance(result, dict) else {}
        items = r.get("items", r.get("holdings", [])) or []
        rows = []
        for it in items:
            if not isinstance(it, dict):
                continue
            row = dict(it)
            # Canonical key bridges (only fill if absent).
            row.setdefault("evaluationAmount",
                           it.get("evaluationAmount", it.get("marketValue")))
            row.setdefault("averagePrice",
                           it.get("averagePrice", it.get("purchasePrice")))
            row.setdefault("currentPrice", it.get("currentPrice", it.get("price")))
            row.setdefault("profitLoss",
                           it.get("profitLoss", it.get("profitLossAmount")))
            row.setdefault("profitLossRate",
                           it.get("profitLossRate", it.get("profitLossRatio")))
            # Per-row 오늘 손익. Live items may carry dailyProfitLoss either as a
            # nested {amount:{krw,usd}, rate} node or a flat scalar — flatten to a
            # canonical scalar string + a rate so portfolio tools can show 오늘 손익.
            dp = it.get("dailyProfitLoss")
            if isinstance(dp, dict):
                amt = dp.get("amount", dp)
                if isinstance(amt, dict):
                    amt = amt.get("krw", amt.get("usd"))
                # Overwrite (not setdefault): row already copied the nested dict.
                row["dailyProfitLoss"] = amt
                row.setdefault("dailyProfitLossRate", dp.get("rate"))
            elif dp is not None:
                row.setdefault("dailyProfitLoss", dp)
            rows.append(row)

        def _amt(node, ccy="krw"):
            if isinstance(node, dict):
                v = node.get(ccy, node.get("amount"))
                if isinstance(v, dict):
                    v = v.get(ccy)
                return v
            return node

        mv = r.get("marketValue") or {}
        tp = r.get("totalPurchaseAmount") or {}
        pl = r.get("profitLoss") or {}
        dpl = r.get("dailyProfitLoss") or {}
        summary = {
            "totalEvaluationKRW": _amt(mv.get("amount", mv)),
            "totalCostKRW": _amt(tp),
            "totalProfitLossKRW": _amt(pl.get("amount", pl)),
            "dailyProfitLossKRW": _amt(dpl.get("amount", dpl)),
            "dailyProfitLossRate": (dpl.get("rate") if isinstance(dpl, dict) else None),
            "profitLossRate": (pl.get("rate") if isinstance(pl, dict) else None),
        }
        return {"holdings": rows, "summary": summary,
                "_liveTotals": {"marketValue": mv, "totalPurchaseAmount": tp,
                                "profitLoss": pl, "dailyProfitLoss": dpl},
                "_mode": "live"}


# ---------------------------------------------------------------------------
# The client facade
# ---------------------------------------------------------------------------


class TossClient:
    """Single entry point every tool uses. Routes mock vs live transparently."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.mode = cfg.mode
        self._rate = RateGuard()
        if cfg.live:
            self._tokens: Optional[TokenManager] = TokenManager(cfg)
            self._mock: Optional[MockData] = None
            LOG.info("TossClient initialised in LIVE mode (base=%s)", cfg.base_url)
        else:
            self._tokens = None
            self._mock = get_mock(cfg.stock_dashboard_dir)
            LOG.info("TossClient initialised in MOCK mode (no network)")
        self._account_seq: Optional[int] = None

    # ----- low-level HTTP (live only) ------------------------------------
    def _http(self, method: str, path: str, group: str = "MARKET_DATA",
              params: Optional[Dict] = None, body: Optional[Dict] = None,
              account: bool = False, max_retries: int = 3) -> Dict:
        if not self.cfg.live:
            raise RuntimeError("BUG: _http called in mock mode (socket blocked)")
        assert self._tokens is not None
        url = f"{self.cfg.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None}
            )
        headers = {
            "Authorization": f"Bearer {self._tokens.get_token()}",
            "Accept": "application/json",
        }
        if account:
            headers["X-Tossinvest-Account"] = str(self.account_seq())
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            attempt += 1
            self._rate.before(group)
            req = urllib.request.Request(url, data=data, method=method,
                                         headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    self._rate.observe(group, dict(resp.headers))
                    # M-NEW-1: once the request went OUT, a failure while
                    # READING/PARSING the response leaves the order outcome
                    # UNKNOWN — the broker may have accepted it. For POST,
                    # surface that as TossNetworkUncertain (reconcile, never
                    # blind-retry / never release the idempotency claim).
                    try:
                        raw = resp.read().decode("utf-8")
                        return self._parse_envelope(json.loads(raw) if raw else {})
                    except (TossApiError, TossNetworkUncertain):
                        raise
                    except Exception as rexc:  # timeout/decode/parse mid-read
                        if method == "POST":
                            raise TossNetworkUncertain(
                                f"POST {path} response read/parse failed: "
                                f"{type(rexc).__name__}") from rexc
                        raise
            except urllib.error.HTTPError as e:
                hdrs = dict(getattr(e, "headers", {}) or {})
                self._rate.observe(group, hdrs)
                if e.code in (429, 500, 502, 503, 504) and attempt <= max_retries:
                    if method == "POST":
                        # Uncertain order outcome -> never blindly retry POST.
                        raise TossNetworkUncertain(f"POST {path} HTTP {e.code}")
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise self._http_error(e)
            except urllib.error.URLError as e:
                if method == "POST":
                    raise TossNetworkUncertain(f"POST {path} {e.reason}")
                if attempt <= max_retries:
                    time.sleep(min(2 ** attempt, 8))
                    continue
                raise TossApiError("network-error", str(e.reason))

    @staticmethod
    def _http_error(e: urllib.error.HTTPError) -> TossApiError:
        try:
            payload = json.loads(e.read().decode("utf-8"))
        except Exception:
            return TossApiError(f"http-{e.code}", "", status=e.code)
        # Two documented error shapes (openapi.json v1.1.1):
        #   API    : {error:{requestId, code, message, data?}}
        #   OAuth  : {error, error_description}
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            # OAuth error: {error:"invalid_client", error_description:"..."}
            raw = payload.get("error", f"http-{e.code}")
            return TossApiError(
                ERROR_CODE_MAP.get(raw, raw),
                payload.get("error_description", ""), status=e.code)
        err = payload.get("error", payload) if isinstance(payload, dict) else {}
        if not isinstance(err, dict):
            err = {}
        raw_code = err.get("code", f"http-{e.code}")
        return TossApiError(
            ERROR_CODE_MAP.get(raw_code, raw_code),
            err.get("message", ""), err.get("data"), e.code,
            request_id=err.get("requestId"))

    @staticmethod
    def _parse_envelope(payload: Dict) -> Dict:
        """Unwrap BFF ApiResponse {result|error}. Tag _mode=live.

        Live error envelope is ``{error:{requestId, code, message, data?}}``
        (openapi.json v1.1.1); the documented ``code`` is mapped to our standard
        code via :data:`ERROR_CODE_MAP` and ``requestId`` is preserved.
        """
        if isinstance(payload, dict) and "error" in payload and payload["error"]:
            err = payload["error"]
            if isinstance(err, str):  # OAuth-style error in a body (rare)
                raise TossApiError(ERROR_CODE_MAP.get(err, err),
                                   payload.get("error_description", ""))
            raw_code = err.get("code", "unknown")
            raise TossApiError(ERROR_CODE_MAP.get(raw_code, raw_code),
                               err.get("message", ""), err.get("data"),
                               request_id=err.get("requestId"))
        result = payload.get("result", payload) if isinstance(payload, dict) else payload
        if isinstance(result, dict):
            result.setdefault("_mode", "live")
        return result

    # ----- account context ----------------------------------------------
    def account_seq(self) -> int:
        if self._account_seq is not None:
            return self._account_seq
        raw = self.get_accounts()
        # get_accounts() now always returns the canonical {accounts:[...]} shape
        # (live array is normalized upstream), but stay defensive against a bare
        # list slipping through so this never crashes with `.get` on a list.
        if isinstance(raw, list):
            accounts = raw
        elif isinstance(raw, dict):
            accounts = raw.get("accounts", []) or []
        else:
            accounts = []
        idx = self.cfg.account_index
        if not accounts:
            raise TossApiError("no-account", "no normal accounts found")
        idx = max(0, min(idx, len(accounts) - 1))
        self._account_seq = int(accounts[idx]["accountSeq"])
        return self._account_seq

    # =====================================================================
    # Public API methods (each branches mock vs live exactly once)
    # =====================================================================

    def logout(self) -> Dict:
        """Drop the cached OAuth token (memory + token.json). No-op in mock."""
        if self._tokens is not None:
            out = self._tokens.logout()
            out["_mode"] = "live"
            return out
        return {"tokenCleared": True, "fileDeleted": False, "_mode": "mock"}

    # ----- market data ----
    def get_price(self, symbol: str) -> Dict:
        if self._mock:
            return self._mock.price(symbol)
        # Official spec documents only `symbols` (comma list) for /prices; a
        # single quote is fetched as symbols=<sym> and the first row is taken.
        raw = self._http("GET", "/api/v1/prices", "MARKET_DATA",
                         params={"symbols": symbol})
        return _Normalize.price(raw, symbol)

    def get_prices(self, symbols: List[str]) -> Dict:
        if self._mock:
            return self._mock.prices(symbols)
        raw = self._http("GET", "/api/v1/prices", "MARKET_DATA",
                         params={"symbols": ",".join(symbols)})
        return _Normalize.prices(raw)

    def get_orderbook(self, symbol: str) -> Dict:
        if self._mock:
            return self._mock.orderbook(symbol)
        raw = self._http("GET", "/api/v1/orderbook", "MARKET_DATA",
                         params={"symbol": symbol})
        return _Normalize.orderbook(raw, symbol)

    def get_candles(self, symbol: str, interval: str = "1d", count: int = 100,
                    before: Optional[str] = None, adjusted: bool = True) -> Dict:
        if self._mock:
            return self._mock.candles(symbol, interval, count, before)
        raw = self._http("GET", "/api/v1/candles", "MARKET_DATA_CHART",
                         params={"symbol": symbol, "interval": interval,
                                 "count": count, "before": before,
                                 "adjusted": str(adjusted).lower()})
        return _Normalize.candles(raw, symbol, interval)

    def get_trades(self, symbol: str, count: int = 50) -> Dict:
        if self._mock:
            return self._mock.trades(symbol, count)
        raw = self._http("GET", "/api/v1/trades", "MARKET_DATA",
                         params={"symbol": symbol, "count": count})
        return _Normalize.trades(raw, symbol)

    def get_price_limits(self, symbol: str) -> Dict:
        if self._mock:
            return self._mock.price_limits(symbol)
        raw = self._http("GET", "/api/v1/price-limits", "MARKET_DATA",
                         params={"symbol": symbol})
        return _Normalize.price_limits(raw, symbol)

    def get_warnings(self, symbol: str) -> Dict:
        if self._mock:
            return self._mock.warnings(symbol)
        raw = self._http("GET", f"/api/v1/stocks/{symbol}/warnings", "STOCK")
        return _Normalize.warnings(raw, symbol)

    def get_stocks(self, symbols: List[str]) -> Dict:
        """Stock master data for one or more symbols (GET /api/v1/stocks).

        Returns canonical ``{stocks:[...]}``. ``symbols`` is comma-joined for the
        ``symbols`` query param (pattern ``^[A-Za-z0-9.,\\-]+$``); empties dropped.
        """
        if self._mock:
            return self._mock.stocks(symbols)
        syms = [s.strip() for s in symbols if s and s.strip()]
        raw = self._http("GET", "/api/v1/stocks", "STOCK",
                         params={"symbols": ",".join(syms)})
        return _Normalize.stocks(raw)

    def tick_size(self, symbol: str, price: float) -> float:
        # Tick ladder is identical in mock/live (KRX rule); reuse mock helper.
        helper = self._mock or get_mock(self.cfg.stock_dashboard_dir)
        return helper.tick_size(symbol, price)

    def currency_for(self, symbol: str) -> str:
        helper = self._mock or get_mock(self.cfg.stock_dashboard_dir)
        return helper.currency_for(symbol)

    def resolve_symbol(self, query: str) -> List[Dict[str, str]]:
        helper = self._mock or get_mock(self.cfg.stock_dashboard_dir)
        return helper.resolve(query)

    # ----- reference ----
    def get_exchange_rate(self, base: str, quote: str,
                          datetime_: Optional[str] = None) -> Dict:
        if self._mock:
            return self._mock.exchange_rate(base, quote)
        raw = self._http("GET", "/api/v1/exchange-rate", "MARKET_INFO",
                         params={"baseCurrency": base, "quoteCurrency": quote,
                                 "datetime": datetime_})
        return _Normalize.exchange_rate(raw, base, quote)

    def get_market_calendar(self, region: str, date: Optional[str] = None) -> Dict:
        if self._mock:
            return self._mock.market_calendar(region, date)
        raw = self._http("GET", f"/api/v1/market-calendar/{region.upper()}",
                         "MARKET_INFO", params={"date": date})
        return _Normalize.market_calendar(raw, region)

    # ----- account ----
    def get_accounts(self) -> Dict:
        if self._mock:
            return self._mock.accounts()
        raw = self._http("GET", "/api/v1/accounts", "ACCOUNT")
        return _Normalize.accounts(raw)

    def get_holdings(self, symbol: Optional[str] = None) -> Dict:
        if self._mock:
            return self._mock.holdings(symbol)
        raw = self._http("GET", "/api/v1/holdings", "ASSET",
                         params={"symbol": symbol}, account=True)
        return _Normalize.holdings(raw)

    def get_buying_power(self, currency: str) -> Dict:
        if self._mock:
            return self._mock.buying_power(currency)
        raw = self._http("GET", "/api/v1/buying-power", "ORDER_INFO",
                         params={"currency": currency}, account=True)
        return _Normalize.buying_power(raw, currency)

    def get_sellable_quantity(self, symbol: str) -> Dict:
        if self._mock:
            return self._mock.sellable_quantity(symbol)
        raw = self._http("GET", "/api/v1/sellable-quantity", "ORDER_INFO",
                         params={"symbol": symbol}, account=True)
        return _Normalize.sellable_quantity(raw, symbol)

    def get_commissions(self) -> Dict:
        if self._mock:
            return self._mock.commissions()
        raw = self._http("GET", "/api/v1/commissions", "ORDER_INFO", account=True)
        return _Normalize.commissions(raw)

    # ----- orders (READ) ----
    def list_orders(self, status: str = "OPEN", **params) -> Dict:
        if self._mock:
            return self._mock.list_orders(status)
        return self._http("GET", "/api/v1/orders", "ORDER_HISTORY",
                          params={"status": status, **params}, account=True)

    def get_order(self, order_id: Optional[str] = None,
                  client_order_id: Optional[str] = None) -> Dict:
        if self._mock:
            return self._mock.get_order(order_id, client_order_id)
        if order_id:
            return self._http("GET", f"/api/v1/orders/{order_id}",
                              "ORDER_HISTORY", account=True)
        return self._http("GET", "/api/v1/orders", "ORDER_HISTORY",
                          params={"clientOrderId": client_order_id}, account=True)

    # ----- orders (WRITE) — guarded by server.py before reaching here ----
    # Official OrderCreateRequest (openapi.json): the broker accepts ONLY these
    # keys, and quantity/orderAmount/price are STRINGS ("70000", "0.5"), never
    # JSON numbers, and there is NO market/currency field (symbol implies it).
    # The internal snapshot carries many extra fields (warnings/highValue/
    # estimatedAmount/_gateWarnings/account_index…) that must NEVER be POSTed.
    _ORDER_BODY_KEYS = ("clientOrderId", "symbol", "side", "orderType",
                        "timeInForce", "quantity", "orderAmount", "price",
                        "confirmHighValueOrder")
    _MODIFY_BODY_KEYS = ("clientOrderId", "timeInForce", "quantity",
                         "orderAmount", "price", "confirmHighValueOrder")
    _ORDER_NUM_STR_KEYS = frozenset({"quantity", "orderAmount", "price"})

    @staticmethod
    def _num_to_str(v) -> str:
        """Render an order number as the broker's string form: '70000', '0.5'
        (no trailing zeros, no scientific notation, no '70000.0')."""
        if isinstance(v, str):
            return v
        f = float(v)
        if f == int(f):
            return str(int(f))
        return f"{f:.6f}".rstrip("0").rstrip(".")

    @classmethod
    def _order_body(cls, snapshot: Dict, keys) -> Dict:
        body: Dict = {}
        for k in keys:
            v = snapshot.get(k)
            if v is None:
                continue
            body[k] = cls._num_to_str(v) if k in cls._ORDER_NUM_STR_KEYS else v
        return body

    def place_order(self, snapshot: Dict) -> Dict:
        self._assert_can_write()
        if self._mock:
            return self._mock.place_order(snapshot)
        body = self._order_body(snapshot, self._ORDER_BODY_KEYS)
        return self._post_with_reconcile(
            "/api/v1/orders", body,
            client_order_id=body.get("clientOrderId"))

    def modify_order(self, order_id: str, snapshot: Dict) -> Dict:
        self._assert_can_write()
        if self._mock:
            return self._mock.modify_order(order_id, snapshot)
        body = self._order_body(snapshot, self._MODIFY_BODY_KEYS)
        return self._post_with_reconcile(
            f"/api/v1/orders/{order_id}/modify", body,
            client_order_id=body.get("clientOrderId"))

    def cancel_order(self, order_id: str, client_order_id: Optional[str] = None) -> Dict:
        self._assert_can_write()
        if self._mock:
            return self._mock.cancel_order(order_id)
        return self._post_with_reconcile(
            f"/api/v1/orders/{order_id}/cancel", {}, client_order_id=client_order_id)

    def _assert_can_write(self) -> None:
        # Re-check the kill switch from the live env immediately before any POST.
        if kill_active():
            raise TossApiError("kill-switch", "TOSS_KILL=1 — orders disabled")
        if self.cfg.live and not self.cfg.allow_live_orders:
            raise TossApiError(
                "orders-disabled",
                "TOSS_ALLOW_LIVE_ORDERS!=1 — live order transport off")

    def _post_with_reconcile(self, path: str, body: Dict,
                             client_order_id: Optional[str]) -> Dict:
        try:
            return self._http("POST", path, "ORDER", body=body, account=True)
        except TossNetworkUncertain as exc:
            LOG.warning("order POST uncertain (%s) — reconciling, NOT re-POSTing",
                        type(exc).__name__)
            if client_order_id:
                try:
                    found = self.get_order(client_order_id=client_order_id)
                    found["_reconciled"] = True
                    return found
                except Exception:
                    pass
            raise TossApiError(
                "reconcile-required",
                "order outcome unknown; check track_order before retrying")
        except TossApiError as exc:
            # 409 Conflict == already accepted (idempotent) == success.
            if exc.status == 409 or exc.code in {"request-in-progress"}:
                found = self.get_order(client_order_id=client_order_id) if client_order_id else {}
                found["_idempotent_conflict"] = True
                return found
            raise


class Reconciler:
    """Convenience wrapper around TossClient.get_order for post-POST checks."""

    def __init__(self, client: TossClient):
        self._c = client

    def confirm(self, order_id: Optional[str] = None,
                client_order_id: Optional[str] = None) -> Dict:
        return self._c.get_order(order_id=order_id, client_order_id=client_order_id)
