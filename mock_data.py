"""mock_data.py — Deterministic synthetic data for the offline (mock) mode.

When no Toss key is configured (the current state), every tool resolves through
this module instead of the network. The shapes mirror the real Toss Open API
BFF envelopes (string prices, ISO8601 timestamps, integer accountSeq, opaque
order ids, the 10-value OrderStatus enum, partial fills, tax/settlementDate,
etc.) so that flipping to ``live`` later requires zero tool-code changes.

Hard guarantees:
- NO network. This module never opens a socket.
- Every payload carries ``_mode="mock"`` and a ``MOCK`` marker so a human can
  never confuse a simulated fill with a real one.
- Quotes are seeded from the real ``~/stock-dashboard/watchlist.json`` (200
  names) but all *prices* are synthetic and clearly fake.
- No real tokens, account numbers, or secrets ever appear here.

Determinism (critical for golden-value QA regression)
-----------------------------------------------------
The market backend is *purely a function of the symbol* (plus the global day
seed). The same symbol always yields the same price, the same 250-bar daily
series, the same intraday 1m series, the same orderbook, and the same tape —
on every call, in any order. This is what lets ``analyze_symbol`` /
``scalp_score`` / ``intraday_vwap`` produce stable golden values, and lets
``orderbook_pressure`` / ``portfolio_risk_xray`` / ``pnl_attribution`` /
``impact_of_order`` be regression-tested. We never seed off ``datetime.now``
for *values* (only for display timestamps). A per-symbol ``random.Random`` is
used so two symbols are independent and adding a symbol cannot perturb another.

Optional fixtures
-----------------
If ``fixtures/golden_candles_<symbol>.json`` exists (Toss candle response
shape), the daily/intraday series for that symbol is served verbatim from the
fixture instead of being synthesised. This pins the canonical QA symbols to an
externally reviewable file. Absence of fixtures is fine — synthesis is fully
deterministic on its own.

Pure stdlib.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

KST = timezone(timedelta(hours=9))
MOCK = "MOCK"

# Global day-seed. Bumping this regenerates every synthetic series at once
# (keep stable across a QA cycle so golden values do not drift).
DAY_SEED = 20260615

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

# ---------------------------------------------------------------------------
# Watchlist seed (real names, synthetic prices)
# ---------------------------------------------------------------------------

# Yahoo-style market suffix -> Toss region.
_MARKET_TO_REGION = {"KS": "KR", "KQ": "KR", "US": "US", "": "KR"}

# A few US names so mock can exercise USD / amount-based orders too.
_US_SEED = [
    {"code": "NVDA", "name": "엔비디아", "market": "US"},
    {"code": "AAPL", "name": "애플", "market": "US"},
    {"code": "TSLA", "name": "테슬라", "market": "US"},
    {"code": "MSFT", "name": "마이크로소프트", "market": "US"},
]

# Always-resolvable KR names so mock can demo even with no watchlist.json.
_KR_SEED = [
    {"code": "005930", "name": "삼성전자", "market": "KS"},
    {"code": "000660", "name": "SK하이닉스", "market": "KS"},
    {"code": "035720", "name": "카카오", "market": "KS"},
    {"code": "247540", "name": "에코프로비엠", "market": "KQ"},
]


def _load_watchlist(stock_dashboard_dir: Path) -> List[Dict[str, str]]:
    path = stock_dashboard_dir / "watchlist.json"
    items: List[Dict[str, str]] = []
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            for it in data:
                code = str(it.get("code", "")).strip()
                if not code:
                    continue
                items.append(
                    {
                        "code": code,
                        "name": str(it.get("name", code)),
                        "market": str(it.get("market", "KS")),
                    }
                )
    except Exception:
        items = []
    # Guarantee a stable demo set even when watchlist.json is absent.
    have = {it["code"] for it in items}
    for seed in _KR_SEED + _US_SEED:
        if seed["code"] not in have:
            items.append(dict(seed))
            have.add(seed["code"])
    return items


def _sym_hash(symbol: str) -> int:
    return int(hashlib.sha256(f"{symbol}|{DAY_SEED}".encode("utf-8")).hexdigest(), 16)


def _seeded_base_price(symbol: str, currency: str) -> float:
    """Deterministic-but-fake base price from the symbol hash."""
    h = int(hashlib.sha256(symbol.encode("utf-8")).hexdigest(), 16)
    if currency == "USD":
        return round(20 + (h % 90000) / 100.0, 2)      # ~$20 - $920
    return float(((h % 480) + 5) * 1000)               # ~5,000 - 485,000 KRW


def _now_iso() -> str:
    return datetime.now(KST).replace(microsecond=0).isoformat()


def _tick_size(price: float, currency: str) -> float:
    """A simplified KRX/US tick-size model (for plan sanity-checks only)."""
    if currency == "USD":
        return 0.01
    # Korean cash-equity tick ladder (post-2023 simplified).
    if price < 2000:
        return 1
    if price < 5000:
        return 5
    if price < 20000:
        return 10
    if price < 50000:
        return 50
    if price < 200000:
        return 100
    if price < 500000:
        return 500
    return 1000


def _round_px(value: float, currency: str) -> float:
    return round(value, 2) if currency == "USD" else float(round(value))


class MockData:
    """Stateful mock backend data source (in-memory, per-process).

    Market data is *deterministic per symbol*: it is generated once, cached, and
    served identically on every call. Only orders (place/modify/cancel) carry
    mutable session state.
    """

    def __init__(self, stock_dashboard_dir: Path, seed: int = DAY_SEED):
        self._seed = seed
        # Session-only RNG, used solely for opaque order ids (not market data).
        self._rng = random.Random(seed)
        self._watch = _load_watchlist(stock_dashboard_dir)
        self._by_code = {w["code"]: w for w in self._watch}
        # name (lowercased) -> code, for resolve_symbol
        self._by_name = {w["name"].lower(): w["code"] for w in self._watch}
        # Simulated open orders, keyed by orderId.
        self._orders: Dict[str, Dict] = {}
        self._order_seq = 1000
        # Per-symbol caches (deterministic, computed lazily once).
        self._daily_cache: Dict[Tuple[str, int], List[Dict]] = {}
        self._intraday_cache: Dict[str, List[Dict]] = {}
        self._fixture_cache: Dict[str, Optional[List[Dict]]] = {}

    # ----- meta -----------------------------------------------------------
    def _wrap(self, payload: Dict) -> Dict:
        payload.setdefault("_mode", "mock")
        payload.setdefault("_marker", MOCK)
        return payload

    def _sym_rng(self, symbol: str, salt: str = "") -> random.Random:
        """A per-symbol RNG: stable, independent across symbols/streams."""
        return random.Random(_sym_hash(f"{symbol}|{salt}"))

    def currency_for(self, symbol: str) -> str:
        w = self._by_code.get(symbol)
        if w and _MARKET_TO_REGION.get(w["market"], "KR") == "US":
            return "USD"
        # Heuristic: pure-digit -> KRX -> KRW, else US.
        return "KRW" if symbol.isdigit() else "USD"

    def region_for(self, symbol: str) -> str:
        return "US" if self.currency_for(symbol) == "USD" else "KR"

    # ----- deterministic price model -------------------------------------
    def _last_price(self, symbol: str) -> float:
        """The single deterministic 'current price' for a symbol.

        Derived from the last close of the daily series so price, candles,
        orderbook, trades and indicators are all mutually consistent.
        """
        currency = self.currency_for(symbol)
        daily = self._daily(symbol, 250)
        last_close = float(daily[-1]["close"]) if daily else _seeded_base_price(symbol, currency)
        return _round_px(last_close, currency)

    # ----- market data ----------------------------------------------------
    def price(self, symbol: str) -> Dict:
        currency = self.currency_for(symbol)
        last = self._last_price(symbol)
        daily = self._daily(symbol, 250)
        prev = float(daily[-2]["close"]) if len(daily) >= 2 else last
        change = _round_px(last - prev, currency)
        change_pct = round((last - prev) / prev * 100, 2) if prev else 0.0
        vol = int(daily[-1]["volume"]) if daily else 0
        return self._wrap(
            {
                "symbol": symbol,
                "timestamp": _now_iso(),
                "lastPrice": f"{last}",
                "previousClose": f"{_round_px(prev, currency)}",
                "change": f"{change}",
                "changePct": change_pct,
                "volume": vol,
                "currency": currency,
            }
        )

    def prices(self, symbols: List[str]) -> Dict:
        return self._wrap(
            {"prices": [self.price(s) for s in symbols if s], "_mode": "mock"}
        )

    def orderbook(self, symbol: str) -> Dict:
        """10-level depth. Bid/ask quantities are deterministic and *skewed*
        per symbol so ``orderbook_pressure`` can compute a stable imbalance
        ratio. Total bid/ask volume + best spread surfaced for convenience."""
        currency = self.currency_for(symbol)
        base = self._last_price(symbol)
        tick = _tick_size(base, currency)
        rng = self._sym_rng(symbol, "orderbook")
        # Per-symbol persistent skew in [-0.4, +0.4]: + => buy-heavy book.
        skew = (rng.random() - 0.5) * 0.8
        bids, asks = [], []
        bid_total = ask_total = 0
        for i in range(1, 11):
            depth_decay = max(0.25, 1.0 - 0.07 * (i - 1))
            bq = int(max(1, round((1 + skew) * 400 * depth_decay * (0.6 + rng.random()))))
            aq = int(max(1, round((1 - skew) * 400 * depth_decay * (0.6 + rng.random()))))
            bid_total += bq
            ask_total += aq
            bids.append({"price": f"{_round_px(base - tick * i, currency)}",
                         "quantity": bq})
            asks.append({"price": f"{_round_px(base + tick * i, currency)}",
                         "quantity": aq})
        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        return self._wrap(
            {
                "symbol": symbol, "timestamp": _now_iso(),
                "bids": bids, "asks": asks,
                "totalBidQuantity": bid_total, "totalAskQuantity": ask_total,
                "bestBid": f"{best_bid}", "bestAsk": f"{best_ask}",
                "spread": f"{_round_px(best_ask - best_bid, currency)}",
            }
        )

    # ----- deterministic series builders ---------------------------------
    def _fixture(self, symbol: str) -> Optional[List[Dict]]:
        """Load fixtures/golden_candles_<symbol>.json (Toss candle shape) if any."""
        if symbol in self._fixture_cache:
            return self._fixture_cache[symbol]
        path = _FIXTURES_DIR / f"golden_candles_{symbol}.json"
        rows: Optional[List[Dict]] = None
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                rows = data.get("candles") if isinstance(data, dict) else data
        except Exception:
            rows = None
        self._fixture_cache[symbol] = rows
        return rows

    def _daily(self, symbol: str, count: int = 250) -> List[Dict]:
        """Build (once) and cache a deterministic daily OHLCV series.

        ~250 bars by default — enough for ma120, 52-week hi/lo, 1y return,
        slope(140 bars) and the scalpScore inputs. Oldest-first (ascending).
        """
        count = max(1, min(int(count or 250), 400))
        key = (symbol, count)
        if key in self._daily_cache:
            return self._daily_cache[key]

        fx = self._fixture(symbol)
        if fx:
            rows = list(fx)[-count:]
            self._daily_cache[key] = rows
            return rows

        currency = self.currency_for(symbol)
        rng = self._sym_rng(symbol, "daily")
        base = _seeded_base_price(symbol, currency)
        # A gentle trend + wave so arrange / slope / RSI are meaningful and vary
        # by symbol (some 정배열, some 역배열, some 혼조).
        trend = (rng.random() - 0.45) * 0.0018          # per-bar drift
        wave_amp = 0.02 + rng.random() * 0.04           # cyclical component
        wave_period = 30 + rng.randint(0, 60)
        vol_base = rng.randint(50_000, 3_000_000)

        out: List[Dict] = []
        # Start ~ a year ago, ascending to today.
        t0 = datetime.now(KST).replace(hour=15, minute=30, second=0, microsecond=0)
        price = base * (0.85 + rng.random() * 0.10)      # start below 'now'
        for i in range(count):
            # Days ago = count-1-i (oldest first).
            day = t0 - timedelta(days=(count - 1 - i))
            wave = math.sin(2 * math.pi * i / wave_period) * wave_amp
            noise = (rng.random() - 0.5) * 0.022
            drift = trend + wave * 0.02 + noise
            o = price
            c = price * (1 + drift)
            hi = max(o, c) * (1 + abs(rng.random()) * 0.015)
            lo = min(o, c) * (1 - abs(rng.random()) * 0.015)
            # Volume spikes occasionally (gives volRatio variety for scalpScore).
            spike = 3.2 if (i % 37 == 0 and i > 0) else (1.6 if rng.random() > 0.9 else 1.0)
            vol = int(vol_base * spike * (0.6 + rng.random() * 0.8))
            out.append(
                {
                    "timestamp": day.isoformat(),
                    "open": f"{_round_px(o, currency)}",
                    "high": f"{_round_px(hi, currency)}",
                    "low": f"{_round_px(lo, currency)}",
                    "close": f"{_round_px(c, currency)}",
                    "volume": vol,
                }
            )
            price = c
        self._daily_cache[key] = out
        return out

    def _intraday(self, symbol: str) -> List[Dict]:
        """Build (once) and cache today's 1-minute series for VWAP.

        Walks from the regular-session open to 'now' (cap one full session) with
        a believable open/high/low/close drift so VWAP, vwapGap and rangePos are
        computable and stable. Oldest-first.
        """
        if symbol in self._intraday_cache:
            return self._intraday_cache[symbol]

        fx_path = _FIXTURES_DIR / f"golden_intraday_{symbol}.json"
        try:
            if fx_path.is_file():
                data = json.loads(fx_path.read_text(encoding="utf-8"))
                rows = data.get("candles") if isinstance(data, dict) else data
                self._intraday_cache[symbol] = list(rows)
                return self._intraday_cache[symbol]
        except Exception:
            pass

        currency = self.currency_for(symbol)
        rng = self._sym_rng(symbol, "intraday")
        daily = self._daily(symbol, 250)
        prev_close = float(daily[-2]["close"]) if len(daily) >= 2 else self._last_price(symbol)
        last = self._last_price(symbol)
        # Session length: 390 KR / 390 US minutes; keep it bounded and stable.
        n = 390
        # Open with a small gap from prev close, then converge toward 'last'.
        gap = (rng.random() - 0.5) * 0.012
        price = prev_close * (1 + gap)
        out: List[Dict] = []
        # Build a deterministic minute walk that *lands* near `last`.
        target = last
        for i in range(n):
            frac = i / max(1, n - 1)
            pull = (target - price) * (0.02 + 0.03 * frac)   # mean-revert to target
            noise = (rng.random() - 0.5) * price * 0.0018
            o = price
            c = price + pull + noise
            hi = max(o, c) * (1 + abs(rng.random()) * 0.0012)
            lo = min(o, c) * (1 - abs(rng.random()) * 0.0012)
            vol = int((rng.random() * 8000 + 500) * (2.0 if i < 30 or i > n - 30 else 1.0))
            t = datetime.now(KST).replace(hour=9, minute=0, second=0, microsecond=0) \
                + timedelta(minutes=i)
            out.append(
                {
                    "timestamp": t.isoformat(),
                    "open": f"{_round_px(o, currency)}",
                    "high": f"{_round_px(hi, currency)}",
                    "low": f"{_round_px(lo, currency)}",
                    "close": f"{_round_px(c, currency)}",
                    "volume": vol,
                }
            )
            price = c
        self._intraday_cache[symbol] = out
        return out

    def candles(
        self, symbol: str, interval: str = "1d", count: int = 100,
        before: Optional[str] = None,
    ) -> Dict:
        """Toss-shaped candle response.

        Deterministic: the underlying 1d/1m series is generated once per symbol
        and sliced here. ``count`` is clamped to the Toss 200 limit; the adapter
        paginates with ``before`` to assemble the full 250-bar daily window.
        """
        count = max(1, min(int(count or 100), 200))
        if interval == "1m":
            full = self._intraday(symbol)
        else:
            full = self._daily(symbol, 250)

        rows = full
        if before:
            rows = [r for r in full if r["timestamp"] < before]
        # Return the most recent `count` bars (ascending), Toss-style.
        out = rows[-count:]
        next_before = out[0]["timestamp"] if out else None
        # nextBefore is only meaningful while older bars remain.
        has_more = bool(out) and out[0]["timestamp"] != full[0]["timestamp"]
        return self._wrap(
            {"symbol": symbol, "interval": interval, "candles": out,
             "nextBefore": next_before if has_more else None}
        )

    def trades(self, symbol: str, count: int = 50) -> Dict:
        """Recent tape, newest-first. Each trade carries side (BUY/SELL) and a
        ``block`` flag on outsized prints, so ``recent_trades_tape`` can mark
        대량체결 and ``orderbook_pressure``/체결강도 can be computed."""
        count = max(1, min(int(count or 50), 50))
        base = self._last_price(symbol)
        currency = self.currency_for(symbol)
        rng = self._sym_rng(symbol, "trades")
        # Per-symbol buy lean -> stable 체결강도 (>100 buy-dominant).
        buy_lean = 0.40 + rng.random() * 0.25            # 0.40-0.65 P(BUY)
        rows = []
        t = datetime.now(KST).replace(microsecond=0)
        for i in range(count):
            side = "BUY" if rng.random() < buy_lean else "SELL"
            # ~1 in 8 prints is a block trade (대량체결).
            is_block = rng.random() > 0.875
            qty = rng.randint(800, 4000) if is_block else rng.randint(1, 400)
            px = base * (1 + (rng.random() - 0.5) * 0.004)
            row = {
                "timestamp": t.isoformat(),
                "price": f"{_round_px(px, currency)}",
                "quantity": qty,
                "side": side,
            }
            if is_block:
                row["block"] = True
            rows.append(row)
            t = t - timedelta(seconds=rng.randint(1, 30))
        return self._wrap({"symbol": symbol, "trades": rows})

    def price_limits(self, symbol: str) -> Dict:
        base = self._last_price(symbol)
        currency = self.currency_for(symbol)
        if currency == "USD":
            # US has no daily price limit; expose generous band.
            return self._wrap(
                {"symbol": symbol, "upperLimitPrice": f"{round(base*2,2)}",
                 "lowerLimitPrice": f"{round(base*0.5,2)}", "note": "US has no fixed daily limit"}
            )
        return self._wrap(
            {"symbol": symbol, "upperLimitPrice": f"{round(base*1.30)}",
             "lowerLimitPrice": f"{round(base*0.70)}"}
        )

    def warnings(self, symbol: str) -> Dict:
        """Deterministically flag a handful of symbols across the four Toss
        warning kinds (VI·투자경고/위험·정리매매·단기과열) so both the normal
        and the warning case are exercisable. Returns the same shape always."""
        h = _sym_hash(symbol)
        bucket = h % 11
        items: List[Dict] = []
        if bucket == 0:
            items = [{"type": "INVESTMENT_WARNING", "label": "투자경고종목(MOCK)",
                      "severity": "high"}]
        elif bucket == 1:
            items = [{"type": "INVESTMENT_RISK", "label": "투자위험종목(MOCK)",
                      "severity": "critical"}]
        elif bucket == 2:
            items = [{"type": "SHORT_TERM_OVERHEAT", "label": "단기과열(MOCK)",
                      "severity": "medium"}]
        elif bucket == 3:
            items = [{"type": "VI_TRIGGERED", "label": "변동성완화장치(VI) 발동(MOCK)",
                      "severity": "medium"}]
        elif bucket == 4:
            items = [{"type": "DESIGNATED_FOR_LIQUIDATION", "label": "정리매매(MOCK)",
                      "severity": "critical"}]
        elif bucket == 5:
            items = [{"type": "INVESTMENT_CAUTION", "label": "투자주의(MOCK)",
                      "severity": "low"}]
        return self._wrap({"symbol": symbol, "warnings": items})

    def tick_size(self, symbol: str, price: float) -> float:
        return _tick_size(price, self.currency_for(symbol))

    def stocks(self, symbols: List[str]) -> Dict:
        """Stock master(s) for GET /api/v1/stocks.

        Deterministic per symbol, mirroring the official openapi.json v1.1.1
        fields. Some symbols are flagged ETF/leveraged/거래정지/정리매매 (derived
        from the SAME ``_sym_hash`` bucket used by ``warnings`` so the warning
        and the koreanMarketDetail stay mutually consistent across the gate)."""
        out: List[Dict] = []
        for sym in symbols:
            sym = (sym or "").strip()
            if not sym:
                continue
            currency = self.currency_for(sym)
            region = self.region_for(sym)
            w = self._by_code.get(sym)
            name = w["name"] if w else (sym.upper())
            last = self._last_price(sym)
            bucket = _sym_hash(sym) % 11
            # ETF / 레버리지 / 거래정지 / 정리매매 cases (deterministic). Buckets are
            # chosen to MIRROR warnings() (bucket 4=정리매매, 1=투자위험) and to
            # leave the golden selfcheck symbols (005930/000660 == bucket 9) NORMAL
            # so the existing order round-trip keeps passing. Suspension lives on
            # bucket 8 ONLY (no golden symbol lands there).
            is_etf = bucket in (6, 7)
            leverage = ("2.0" if bucket == 7 else None)  # 2x leveraged ETF
            security_type = "ETF" if is_etf else "STOCK"
            is_common = not is_etf
            liquidation = (bucket == 4)              # 정리매매 (== warnings bucket 4)
            krx_suspended = (bucket == 8)            # 거래정지(KRX)
            nxt_suspended = (bucket == 8)            # NXT 거래정지 (same bucket)
            status = "DELISTING" if liquidation else (
                "SUSPENDED" if krx_suspended else "NORMAL")
            shares = str(int((_sym_hash(sym) % 900_000_000) + 10_000_000))
            korean_detail = None
            if region == "KR":
                korean_detail = {
                    "liquidationTrading": liquidation,
                    "nxtSupported": True,
                    "krxTradingSuspended": krx_suspended,
                    "nxtTradingSuspended": nxt_suspended,
                }
            out.append({
                "symbol": sym,
                "name": name,
                "englishName": (name if region == "US" else sym),
                "isinCode": (f"KR7{sym}003" if region == "KR" and sym.isdigit()
                             else f"US{sym.upper()}0000"),
                "market": ("KRX" if region == "KR" else "NASDAQ"),
                "securityType": security_type,
                "isCommonShare": is_common,
                "status": status,
                "currency": currency,
                "listDate": "2010-01-04",
                "delistDate": ("2026-07-31" if liquidation else None),
                "sharesOutstanding": shares,
                "leverageFactor": leverage,
                "koreanMarketDetail": korean_detail,
                # convenience: current price echoed so market-cap is one call.
                "currentPrice": f"{_round_px(last, currency)}",
            })
        return self._wrap({"stocks": out})

    # ----- alpha: supply/demand flow (non-Toss; mock-only) ----------------
    def supply_demand_flow(self, symbol: str) -> Dict:
        """외국인/기관/개인 순매수(억원) + 추세.

        Toss does NOT provide this; in live mode it is sourced from
        stock-dashboard (Naver). In MOCK we synthesise a plausible, deterministic
        figure and stamp ``_source: "mock"`` so it can never be mistaken for a
        real feed. KRX-only (US returns empty with a note)."""
        if self.region_for(symbol) != "KR":
            return self._wrap({
                "symbol": symbol, "_source": "mock", "supported": False,
                "note": "외국인/기관 수급은 국내(KRX) 전용 — 미국 종목 미지원",
                "flow": None,
            })
        rng = self._sym_rng(symbol, "flow")
        base = self._last_price(symbol)
        days = []
        f_trend = (rng.random() - 0.45)                  # net foreign bias
        d0 = datetime.now(KST).date()
        for k in range(5):
            day = d0 - timedelta(days=k)
            foreign = round((rng.random() - 0.5 + f_trend) * 800, 1)   # 억원
            inst = round((rng.random() - 0.5) * 500, 1)
            indiv = round(-(foreign + inst), 1)                        # 근사
            days.append({
                "date": day.isoformat(),
                "foreignVal": foreign, "instVal": inst, "indivVal": indiv,
            })
        latest = days[0]
        foreign_5d = round(sum(d["foreignVal"] for d in days), 1)
        trend = ("순매수" if foreign_5d > 50 else
                 "순매도" if foreign_5d < -50 else "중립")
        return self._wrap({
            "symbol": symbol,
            "_source": "mock",
            "supported": True,
            "latest": latest,
            "history": days,
            "foreignNet5d": foreign_5d,
            "foreignTrend": trend,
            "foreignHoldRate": round(20 + (rng.random() * 40), 2),
            "note": "MOCK 합성 수급 — 실데이터 아님. live는 stock-dashboard(Naver) 출처.",
            "_base": f"{base}",
        })

    # ----- reference data -------------------------------------------------
    def exchange_rate(self, base: str, quote: str) -> Dict:
        rate = 1380.0 if (base, quote) == ("USD", "KRW") else (
            1 / 1380.0 if (base, quote) == ("KRW", "USD") else 1.0
        )
        valid_until = (datetime.now(KST) + timedelta(minutes=1)).replace(
            microsecond=0
        ).isoformat()
        return self._wrap(
            {
                "baseCurrency": base,
                "quoteCurrency": quote,
                "rate": f"{round(rate, 4)}",
                "validUntil": valid_until,
                "note": "MOCK display rate — not an execution guarantee",
            }
        )

    def market_calendar(self, region: str, date: Optional[str] = None) -> Dict:
        """Two cases deterministically exercisable: a weekday is OPEN, a weekend
        (or the canned holiday 2026-01-01 / US 2026-07-04) is CLOSED."""
        d = date or datetime.now(KST).date().isoformat()
        region_u = region.upper()
        # Determine open/closed.
        try:
            dt = datetime.fromisoformat(d).date()
        except ValueError:
            dt = datetime.now(KST).date()
        weekend = dt.weekday() >= 5
        kr_holidays = {"2026-01-01", "2026-03-01", "2026-05-05", "2026-08-15"}
        us_holidays = {"2026-01-01", "2026-07-03", "2026-11-26", "2026-12-25"}
        holidays = kr_holidays if region_u == "KR" else us_holidays
        is_holiday = d in holidays
        is_open = not (weekend or is_holiday)

        if region_u == "KR":
            sessions = [
                {"name": "REGULAR", "open": "09:00", "close": "15:30", "tz": "KST"},
                {"name": "NXT", "open": "08:00", "close": "20:00", "tz": "KST",
                 "note": "넥스트레이드(NXT) 연장세션"},
            ]
        else:
            sessions = [
                {"name": "PRE", "open": "18:00", "close": "23:30", "tz": "KST"},
                {"name": "REGULAR", "open": "23:30", "close": "06:00", "tz": "KST"},
                {"name": "AFTER", "open": "06:00", "close": "10:00", "tz": "KST"},
            ]
        return self._wrap(
            {"region": region_u, "date": d, "isOpen": is_open,
             "isHoliday": is_holiday,
             "holidayReason": ("공휴일(MOCK)" if is_holiday else ("주말" if weekend else None)),
             "sessions": sessions if is_open else []}
        )

    # ----- account -------------------------------------------------------
    def accounts(self) -> Dict:
        return self._wrap(
            {
                "accounts": [
                    {"accountSeq": 10001, "accountNo": "MOCK-0000-0001",
                     "accountType": "STOCK", "status": "NORMAL"},
                    {"accountSeq": 10002, "accountNo": "MOCK-0000-0002",
                     "accountType": "PENSION", "status": "NORMAL"},
                ]
            }
        )

    def _holding_rows(self) -> List[Dict]:
        """Deterministic multi-symbol book (KR + US) for portfolio tools.

        Mix of winners and losers across both markets so portfolio_risk_xray
        (concentration / 쏠림), pnl_attribution (기여도 TOP) and impact_of_order
        (what-if) all have something interesting to chew on. currentPrice is
        pulled from the deterministic price model so evaluations are consistent
        with every other tool."""
        spec = [
            # symbol, name, qty, avg, currency
            ("005930", "삼성전자", 30, 71000, "KRW"),
            ("000660", "SK하이닉스", 15, 185000, "KRW"),
            ("035720", "카카오", 50, 48000, "KRW"),
            ("247540", "에코프로비엠", 8, 220000, "KRW"),
            ("NVDA", "엔비디아", 5, 780.50, "USD"),
            ("AAPL", "애플", 12, 195.00, "USD"),
            ("TSLA", "테슬라", 4, 250.00, "USD"),
        ]
        rows: List[Dict] = []
        for symbol, name, qty, avg, currency in spec:
            cur = self._last_price(symbol)
            cost = avg * qty
            evaln = cur * qty
            pl = evaln - cost
            pl_rate = round(pl / cost * 100, 2) if cost else 0.0
            # Stable per-symbol daily P&L slice from price.change.
            day_chg = float(self.price(symbol)["change"])
            daily_pl = round(day_chg * qty, 2 if currency == "USD" else 0)
            # 오늘 등락률(%) — daily P&L over (eval - daily P&L) ≈ prior eval.
            prior_eval = evaln - daily_pl
            daily_rate = round(daily_pl / prior_eval * 100, 2) if prior_eval else 0.0
            rows.append({
                "symbol": symbol, "name": name, "quantity": qty,
                "averagePrice": f"{avg}",
                "currentPrice": f"{_round_px(cur, currency)}",
                "evaluationAmount": f"{_round_px(evaln, currency)}",
                "cost": f"{_round_px(cost, currency)}",
                "profitLoss": f"{_round_px(pl, currency)}",
                "profitLossRate": f"{pl_rate}",
                "dailyProfitLoss": f"{daily_pl}",
                "dailyProfitLossRate": f"{daily_rate}",
                "currency": currency,
                "market": self.region_for(symbol),
            })
        return rows

    def holdings(self, symbol: Optional[str] = None) -> Dict:
        rows = self._holding_rows()
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol]
        # Summary in KRW (US legs converted at the mock display rate).
        fx = 1380.0
        total_eval = total_cost = total_pl = total_daily = 0.0
        for r in rows:
            mult = fx if r["currency"] == "USD" else 1.0
            total_eval += float(r["evaluationAmount"]) * mult
            total_cost += float(r["cost"]) * mult
            total_pl += float(r["profitLoss"]) * mult
            total_daily += float(r["dailyProfitLoss"]) * mult
        pl_rate = round(total_pl / total_cost * 100, 2) if total_cost else 0.0
        prior_total = total_eval - total_daily
        daily_rate = round(total_daily / prior_total * 100, 2) if prior_total else 0.0
        return self._wrap(
            {
                "holdings": rows,
                "summary": {
                    "totalEvaluationKRW": f"{round(total_eval)}",
                    "totalCostKRW": f"{round(total_cost)}",
                    "totalProfitLossKRW": f"{round(total_pl)}",
                    "dailyProfitLossKRW": f"{round(total_daily)}",
                    "dailyProfitLossRate": f"{daily_rate}",
                    "profitLossRate": f"{pl_rate}",
                    "fxRateUSDKRW": f"{fx}",
                },
            }
        )

    def buying_power(self, currency: str) -> Dict:
        amt = "12500000" if currency == "KRW" else "3200.00"
        return self._wrap(
            {"currency": currency, "availableAmount": amt, "withdrawableAmount": amt}
        )

    def sellable_quantity(self, symbol: str) -> Dict:
        owned = {r["symbol"]: r["quantity"] for r in self._holding_rows()}
        qty = owned.get(symbol, 0)
        return self._wrap({"symbol": symbol, "sellableQuantity": qty})

    def commissions(self) -> Dict:
        return self._wrap(
            {
                "policies": [
                    {"market": "KR", "buyRate": "0.0", "sellRate": "0.0",
                     "tax": "0.18", "note": "국내 수수료 면제(2026.6 기준, MOCK)"},
                    {"market": "US", "buyRate": "0.07", "sellRate": "0.07",
                     "tax": "0.0", "note": "MOCK"},
                ]
            }
        )

    # ----- orders --------------------------------------------------------
    def _new_order_id(self) -> str:
        self._order_seq += 1
        # Opaque, variable-length, not sequential-looking.
        raw = f"mock-{self._order_seq}-{self._rng.randint(1000,9999)}"
        return "MOCKORD" + hashlib.sha1(raw.encode()).hexdigest()[:18]

    def list_orders(self, status: str = "OPEN") -> Dict:
        # Provide a couple of canned orders covering the lifecycle.
        sample = [
            {
                "orderId": "MOCKORDexisting0001", "clientOrderId": "MOCKCOID0001",
                "symbol": "005930", "side": "BUY", "orderType": "LIMIT",
                "price": "70000", "quantity": 10, "status": "PARTIAL_FILLED",
                "timeInForce": "DAY", "createdAt": _now_iso(),
                "execution": {
                    "filledQuantity": 4, "averageFilledPrice": "70000",
                    "filledAmount": "280000", "commission": "0", "tax": "0",
                    "settlementDate": "2026-06-17",
                },
            },
            {
                "orderId": "MOCKORDexisting0002", "clientOrderId": "MOCKCOID0002",
                "symbol": "NVDA", "side": "SELL", "orderType": "LIMIT",
                "price": "820.00", "quantity": 2, "status": "FILLED",
                "timeInForce": "DAY", "createdAt": _now_iso(),
                "execution": {
                    "filledQuantity": 2, "averageFilledPrice": "820.00",
                    "filledAmount": "1640.00", "commission": "1.15", "tax": "0.0",
                    "settlementDate": "2026-06-17",
                },
            },
        ]
        if status.upper() == "OPEN":
            rows = [o for o in sample if o["status"] in
                    {"PENDING", "PARTIAL_FILLED", "PENDING_CANCEL", "PENDING_REPLACE"}]
        else:
            rows = [o for o in sample if o["status"] in
                    {"FILLED", "CANCELED", "REJECTED"}]
        # Include any live-session simulated orders too.
        rows = rows + list(self._orders.values())
        return self._wrap({"orders": rows, "nextCursor": None, "hasNext": False})

    def get_order(self, order_id: Optional[str] = None,
                  client_order_id: Optional[str] = None) -> Dict:
        if order_id and order_id in self._orders:
            return self._wrap(dict(self._orders[order_id]))
        # Fall back to a canned one for the reconcile path demo.
        for o in self.list_orders("OPEN")["orders"] + self.list_orders("CLOSED")["orders"]:
            if order_id and o.get("orderId") == order_id:
                return self._wrap(dict(o))
            if client_order_id and o.get("clientOrderId") == client_order_id:
                return self._wrap(dict(o))
        return self._wrap({"error": {"code": "not-found",
                                     "message": "MOCK: order not found"}})

    def place_order(self, snapshot: Dict) -> Dict:
        """Simulate POST /orders from a frozen plan snapshot."""
        oid = self._new_order_id()
        order = {
            "orderId": oid,
            "clientOrderId": snapshot.get("clientOrderId", "MOCKCOID"),
            "symbol": snapshot.get("symbol"),
            "side": snapshot.get("side"),
            "orderType": snapshot.get("orderType", "LIMIT"),
            "price": snapshot.get("price"),
            "quantity": snapshot.get("quantity"),
            "orderAmount": snapshot.get("orderAmount"),
            "status": "PENDING",
            "timeInForce": snapshot.get("timeInForce", "DAY"),
            "createdAt": _now_iso(),
            "execution": {
                "filledQuantity": 0, "averageFilledPrice": None,
                "filledAmount": "0", "commission": "0", "tax": "0",
                "settlementDate": None,
            },
        }
        self._orders[oid] = order
        return self._wrap(dict(order))

    def modify_order(self, order_id: str, snapshot: Dict) -> Dict:
        old = self._orders.pop(order_id, None)
        new_id = self._new_order_id()   # Toss returns a *new* orderId on modify.
        new = dict(old or {"symbol": snapshot.get("symbol"), "side": snapshot.get("side")})
        new.update(
            {
                "orderId": new_id,
                "price": snapshot.get("price", new.get("price")),
                "quantity": snapshot.get("quantity", new.get("quantity")),
                "status": "PENDING_REPLACE",
                "replacedFrom": order_id,
                "createdAt": _now_iso(),
            }
        )
        self._orders[new_id] = new
        return self._wrap(dict(new))

    def cancel_order(self, order_id: str) -> Dict:
        old = self._orders.pop(order_id, None)
        new_id = self._new_order_id()   # Toss returns a *new* orderId on cancel.
        new = dict(old or {"symbol": "UNKNOWN"})
        new.update(
            {"orderId": new_id, "status": "PENDING_CANCEL",
             "canceledFrom": order_id, "createdAt": _now_iso()}
        )
        self._orders[new_id] = new
        return self._wrap(dict(new))

    # ----- symbol resolution --------------------------------------------
    def resolve(self, query: str) -> List[Dict[str, str]]:
        q = (query or "").strip()
        if not q:
            return []
        candidates: List[Dict[str, str]] = []
        # Exact code match.
        if q in self._by_code:
            w = self._by_code[q]
            candidates.append(self._cand(w))
        # Exact / partial name match.
        ql = q.lower()
        for name, code in self._by_name.items():
            if ql == name or ql in name:
                candidates.append(self._cand(self._by_code[code]))
        # Bare ticker passthrough (e.g. NVDA not in watchlist).
        if not candidates and (q.isalpha() or q.isdigit()):
            candidates.append(
                {"symbol": q.upper(), "name": q.upper(),
                 "market": "US" if q.isalpha() else "KR",
                 "currency": "USD" if q.isalpha() else "KRW"}
            )
        # De-dup by symbol.
        seen, out = set(), []
        for c in candidates:
            if c["symbol"] not in seen:
                seen.add(c["symbol"])
                out.append(c)
        return out

    def _cand(self, w: Dict[str, str]) -> Dict[str, str]:
        region = _MARKET_TO_REGION.get(w["market"], "KR")
        return {
            "symbol": w["code"], "name": w["name"], "market": region,
            "currency": "USD" if region == "US" else "KRW",
        }

    # ----- watchlist (for rank_watchlist) --------------------------------
    def watchlist(self, limit: Optional[int] = None) -> List[Dict[str, str]]:
        """The resolved watchlist universe (cap-ordered) for rank_watchlist."""
        rows = [self._cand(w) for w in self._watch]
        return rows[:limit] if limit else rows


def get_mock(stock_dashboard_dir: Path) -> MockData:
    return MockData(stock_dashboard_dir)
