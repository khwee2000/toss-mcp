"""indicators.py — Technical indicators computable from Toss candle data.

This module is the **verified-alpha** port of ``~/stock-dashboard/server.py``'s
``compute()`` / ``scalpScore`` / RSI / SMA stack onto Toss
``/api/v1/candles`` data. Only indicators derivable from Toss-provided OHLCV are
produced here (Wilder RSI, moving averages 20/60/120 + 이평괴리율, 정/역배열 +
기울기, 종합상태 라벨, 거래량배수, 변동성, 52주 위치, 1년 수익률, scalpScore
S~D, 상대강도 RS, 체결강도). Foreign-investor flow (외국인수급) is NOT available
from Toss; callers must source it separately (stock-dashboard's Naver crawl) and
must never fabricate it in live mode.

Backward compatibility (load-bearing):
    The original simple API is preserved exactly so existing callers keep
    working:
      - ``sma(values, period) -> float | None``
      - ``rsi(values, period=14) -> float | None``   (already Wilder)
      - ``trade_strength(trades) -> float | None``
      - ``swing_score(rsi_val, last, ma20, strength) -> int | None``
      - ``compute(candles, trades=None) -> dict``
    ``compute()`` STILL returns the original keys
    (``last, rsi14, ma20, ma60, ma120, tradeStrength, swingScore, note``) and now
    additionally returns the full dashboard alpha (``disp20/60/120, arrange,
    slopePct, slopeDir, status, volRatio, volValue, dayRange, gap, hi52, lo52,
    pos52, ret1y, scalpScore, scalpGrade, bars, changePct, ...``).

Input shape (Toss candle envelope ``candles`` array element), time-ascending
(oldest first), prices may be strings:
    {"timestamp": ISO8601, "open": "x", "high": "x", "low": "x",
     "close": "x", "volume": int}

Pure stdlib. No network, no pandas/numpy (original functions are pure list math).
"""

from __future__ import annotations

from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Coercion helpers (Toss prices arrive as strings)
# ---------------------------------------------------------------------------

def _f(v) -> Optional[float]:
    """Coerce a possibly-string numeric to float, else None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ohlcv(candles: List[Dict]):
    """Split a Toss candle list into index-aligned OHLCV float lists.

    Mirrors the stock-dashboard invariant: a candle whose ``close`` is missing
    is dropped *entirely* (the other arrays drop the same row) so that index
    alignment is preserved — otherwise disp/slope/volRatio silently go wrong.
    Returns ``(closes, volumes, highs, lows, opens)`` (oldest -> newest).
    """
    closes: List[float] = []
    volumes: List[float] = []
    highs: List[float] = []
    lows: List[float] = []
    opens: List[float] = []
    for c in candles or []:
        close = _f(c.get("close"))
        if close is None:
            continue
        closes.append(close)
        v = _f(c.get("volume"))
        volumes.append(v if v is not None else 0.0)
        h = _f(c.get("high"))
        highs.append(h if h is not None else close)
        lo = _f(c.get("low"))
        lows.append(lo if lo is not None else close)
        o = _f(c.get("open"))
        opens.append(o if o is not None else close)
    return closes, volumes, highs, lows, opens


def _closes(candles: List[Dict]) -> List[float]:
    """Back-compat helper: extract just the close series (string-safe)."""
    return _ohlcv(candles)[0]


# ---------------------------------------------------------------------------
# Core series math (signatures preserved)
# ---------------------------------------------------------------------------

def sma(values: List[float], period: int) -> Optional[float]:
    """Simple moving average of the last ``period`` values (None if too short).

    Ported from stock-dashboard ``sma()``; identical semantics. Rounded to 4dp
    to match prior behaviour.
    """
    if not values or len(values) < period or period <= 0:
        return None
    return round(sum(values[-period:]) / period, 4)


def rsi(values: List[float], period: int = 14) -> Optional[float]:
    """Wilder RSI(14). Exact port of stock-dashboard ``rsi()``.

    Wilder smoothing (NOT a simple moving-average RSI): initial average over the
    first ``period`` deltas, then recursive ``avg = (avg*(period-1)+x)/period``.
    Returns None if ``len(values) < period+1``; 100.0 if avg_loss == 0.
    Result rounded to 2dp (prior behaviour).
    """
    if not values or len(values) < period + 1:
        return None
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - (100.0 / (1.0 + rs)), 2)


def trade_strength(trades: List[Dict]) -> Optional[float]:
    """체결강도: buy volume / sell volume * 100 (>100 => buy-dominant).

    trades: list of ``{"side": "BUY"|"SELL", "quantity": n}`` (Toss /trades).
    Signature/semantics preserved from the original.
    """
    buy = sell = 0.0
    for t in trades or []:
        q = _f(t.get("quantity"))
        if q is None:
            continue
        if t.get("side") == "BUY":
            buy += q
        elif t.get("side") == "SELL":
            sell += q
    if sell == 0:
        return None if buy == 0 else 999.9
    return round(buy / sell * 100, 1)


def swing_score(rsi_val: Optional[float], last: Optional[float],
                ma20: Optional[float], strength: Optional[float]) -> Optional[int]:
    """A crude 0-100 단타점수 for orientation only (not advice).

    Preserved verbatim for backward compatibility. The richer, dashboard-grade
    score lives in :func:`scalp_score` (liquidity+volatility+volume+momentum).
    """
    if last is None:
        return None
    score = 50
    if rsi_val is not None:
        if rsi_val < 30:
            score += 20
        elif rsi_val > 70:
            score -= 20
    if ma20 is not None:
        score += 15 if last > ma20 else -15
    if strength is not None:
        score += 10 if strength > 100 else -10
    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Dashboard-grade derived indicators (new, exact ports)
# ---------------------------------------------------------------------------

def disparity(price: Optional[float], ma: Optional[float]) -> Optional[float]:
    """이평괴리율 % = (price - ma) / ma * 100. Port of compute()'s ``disp()``.

    None if price or ma missing / ma == 0.
    """
    if price is None or not ma:
        return None
    return (price - ma) / ma * 100.0


def ma_arrangement(ma20: Optional[float], ma60: Optional[float],
                   ma120: Optional[float]) -> Optional[str]:
    """정배열/역배열/혼조 from the three SMAs (compute() lines 147-155).

    Returns None if any SMA is missing.
      - 정배열: ma20 > ma60 > ma120
      - 역배열: ma20 < ma60 < ma120
      - 혼조: otherwise
    """
    if ma20 and ma60 and ma120:
        if ma20 > ma60 > ma120:
            return "정배열"
        if ma20 < ma60 < ma120:
            return "역배열"
        return "혼조"
    return None


def ma120_slope(closes: List[float]):
    """120일선 기울기: today's MA120 vs MA120 20 bars ago (compute() 158-163).

    Needs >= 140 closes. Returns ``(slope_pct, slope_dir)`` where slope_dir is
    'up' (>1%), 'down' (<-1%), or 'flat'. Returns ``(None, None)`` if short.
    """
    if len(closes) < 140:
        return None, None
    ma120_now = sum(closes[-120:]) / 120
    ma120_prev = sum(closes[-140:-20]) / 120
    if not ma120_prev:
        return None, None
    slope_pct = (ma120_now - ma120_prev) / ma120_prev * 100.0
    slope_dir = "up" if slope_pct > 1 else ("down" if slope_pct < -1 else "flat")
    return slope_pct, slope_dir


def volume_ratio(volumes: List[float], price: Optional[float]):
    """거래량배수 + 거래대금. Port of compute() lines 167-176.

    Returns ``(vol_ratio, vol_value)``:
      - vol_ratio: today's volume / mean(prior 20 days) (1.0 = normal).
      - vol_value: 거래대금(억원 근사) = today_vol * price / 1e8.
    Either may be None.
    """
    vol_ratio = None
    vol_value = None
    if volumes and volumes[-1]:
        today_vol = volumes[-1]
        base = volumes[-21:-1] if len(volumes) >= 21 else volumes[:-1]
        base = [v for v in base if v]
        if base:
            avg = sum(base) / len(base)
            if avg:
                vol_ratio = today_vol / avg
        if price is not None:
            vol_value = today_vol * price / 1e8
    return vol_ratio, vol_value


def daily_range_pct(highs: List[float], lows: List[float],
                    closes: List[float]) -> Optional[float]:
    """변동성: mean daily range % over last 20 bars (compute() 179-185).

    (high - low) / close * 100 averaged over up to the last 20 bars.
    """
    if not highs or not lows or not closes:
        return None
    n = min(20, len(highs), len(lows), len(closes))
    rng = [(highs[-i] - lows[-i]) / closes[-i] * 100.0
           for i in range(1, n + 1) if closes[-i]]
    if not rng:
        return None
    return sum(rng) / len(rng)


def gap_pct(opens: List[float], closes: List[float]) -> Optional[float]:
    """당일 시가 갭 % = (open - prevClose) / prevClose * 100 (compute() 186-188)."""
    if opens and len(closes) >= 2 and closes[-2]:
        return (opens[-1] - closes[-2]) / closes[-2] * 100.0
    return None


def position_52w(price: Optional[float], closes: List[float]):
    """52주 고/저/레인지내 위치 + 1년 수익률 (compute() 191-193).

    Returns ``(hi52, lo52, pos52, ret1y)``. pos52/ret1y may be None.
    """
    if not closes or price is None:
        return None, None, None, None
    hi52, lo52 = max(closes), min(closes)
    pos52 = (price - lo52) / (hi52 - lo52) * 100.0 if hi52 > lo52 else None
    ret1y = (price - closes[0]) / closes[0] * 100.0 if closes[0] else None
    return hi52, lo52, pos52, ret1y


def overall_status(rsi_val: Optional[float], d120: Optional[float],
                   arrange: Optional[str], slope_dir: Optional[str]) -> str:
    """종합상태 라벨. Exact port of compute() lines 195-207.

    배열이 추세를 우선 규정한다(역배열은 상승추세로, 정배열은 하락추세로 보지 않음).
      - 과열주의: RSI>=75 AND disp120>=15
      - 낙폭과대: RSI<=30
      - 눌림목후보: trend_up AND -5<=disp120<=3 AND RSI<=60
      - 추세이탈주의: trend_down AND -3<=disp120<=6
      - else 중립
    """
    trend_up = arrange == "정배열" or (slope_dir == "up" and arrange != "역배열")
    trend_down = arrange == "역배열" or (slope_dir == "down" and arrange != "정배열")
    status = "중립"
    if rsi_val is not None and d120 is not None:
        if rsi_val >= 75 and d120 >= 15:
            status = "과열주의"
        elif rsi_val <= 30:
            status = "낙폭과대"
        elif trend_up and -5 <= d120 <= 3 and rsi_val <= 60:
            status = "눌림목후보"
        elif trend_down and -3 <= d120 <= 6:
            status = "추세이탈주의"
    return status


def scalp_score(vol_value: Optional[float], day_range: Optional[float],
                vol_ratio: Optional[float], change_pct: Optional[float]):
    """단타 적합도 0~100 + S~D 등급. Exact port of compute() lines 209-221.

    4 axes: 유동성(거래대금) + 변동성(일평균변동폭) + 거래쏠림(거래량배수) +
    당일모멘텀(|등락률|). Returns ``(score:int, grade:str)``.
    NOT advice — orientation only.
    """
    sc = 0
    v = vol_value or 0
    sc += (40 if v >= 5000 else 32 if v >= 2000 else 25 if v >= 1000 else
           15 if v >= 500 else 5 if v >= 100 else 0)
    dr = day_range or 0
    sc += 25 if dr >= 8 else 22 if dr >= 5 else 15 if dr >= 3 else 8 if dr >= 2 else 0
    vr = vol_ratio or 0
    sc += 20 if vr >= 3 else 15 if vr >= 2 else 8 if vr >= 1.5 else 0
    mom = abs(change_pct or 0)
    sc += 15 if mom >= 5 else 10 if mom >= 3 else 5 if mom >= 1 else 0
    grade = ("S" if sc >= 75 else "A" if sc >= 60 else "B" if sc >= 45
             else "C" if sc >= 30 else "D")
    return sc, grade


def relative_strength(ret1y: Optional[float],
                      index_ret1y: Optional[float]) -> Optional[float]:
    """상대강도(RS) = 종목 1년수익률 - 시장(지수) 1년수익률 (%p).

    Per stock-indicators skill: RS is *derived* at tool level from
    ``compute().ret1y`` and ``compute_index().ret1y`` (no new formula invented).
    Returns None if either input missing.
    """
    if ret1y is None or index_ret1y is None:
        return None
    return round(ret1y - index_ret1y, 2)


# ---------------------------------------------------------------------------
# compute() — full dashboard alpha (back-compat keys + extension)
# ---------------------------------------------------------------------------

def compute(candles: List[Dict], trades: Optional[List[Dict]] = None,
            current_price: Optional[float] = None,
            index_ret1y: Optional[float] = None) -> Dict:
    """Full-stack single-symbol indicators from Toss candle data.

    Faithful port of stock-dashboard ``compute()`` (server.py 115-252) using the
    Toss ``/api/v1/candles`` envelope as the data source instead of Yahoo.

    Args:
        candles: list of Toss candle dicts (time-ascending), prices may be
            strings; ``{timestamp, open, high, low, close, volume}``.
        trades: optional Toss ``/trades`` list for 체결강도 (체결강도/tradeStrength).
        current_price: optional live price (from ``/api/v1/prices``) used as the
            current price; falls back to the last close. (= meta.regularMarketPrice)
        index_ret1y: optional market index 1y return (from ``compute_index``-like
            data) to derive 상대강도(RS). None -> rs is null.

    Returns:
        dict with BOTH the original keys (``last, rsi14, ma20, ma60, ma120,
        tradeStrength, swingScore, note``) AND the full dashboard alpha. All
        numeric outputs rounded as in the dashboard.
    """
    closes, volumes, highs, lows, opens = _ohlcv(candles)
    last_close = closes[-1] if closes else None
    price = current_price if current_price is not None else last_close
    prev = closes[-2] if len(closes) > 1 else price

    ma20 = sma(closes, 20)
    ma60 = sma(closes, 60)
    ma120 = sma(closes, 120)

    rsi14 = rsi(closes, 14)
    d20 = disparity(price, ma20)
    d60 = disparity(price, ma60)
    d120 = disparity(price, ma120)

    change = (price - prev) if (price is not None and prev is not None) else None
    change_pct = (change / prev * 100.0) if (change is not None and prev) else None

    arrange = ma_arrangement(ma20, ma60, ma120)
    slope_pct, slope_dir = ma120_slope(closes)
    vol_ratio, vol_value = volume_ratio(volumes, price)
    day_range = daily_range_pct(highs, lows, closes)
    gap = gap_pct(opens, closes)
    hi52, lo52, pos52, ret1y = position_52w(price, closes)
    status = overall_status(rsi14, d120, arrange, slope_dir)
    sc, grade = scalp_score(vol_value, day_range, vol_ratio, change_pct)
    rs = relative_strength(ret1y, index_ret1y)

    strength = trade_strength(trades or []) if trades else None

    return {
        # ----- original (back-compat) keys -----
        "last": round(price) if price is not None else None,
        "rsi14": rsi14,
        "ma20": round(ma20) if ma20 else None,
        "ma60": round(ma60) if ma60 else None,
        "ma120": round(ma120) if ma120 else None,
        "tradeStrength": strength,
        "swingScore": swing_score(rsi14, price, ma20, strength),
        "note": "외국인수급은 토스 미제공 — stock-dashboard(Naver) 별도 출처 필요",
        # ----- dashboard alpha extension -----
        "price": round(price) if price is not None else None,
        "change": round(change) if change is not None else None,
        "changePct": round(change_pct, 2) if change_pct is not None else None,
        "rsi": rsi14,                          # alias matching dashboard field name
        "disp20": round(d20, 2) if d20 is not None else None,
        "disp60": round(d60, 2) if d60 is not None else None,
        "disp120": round(d120, 2) if d120 is not None else None,
        "arrange": arrange,
        "slopePct": round(slope_pct, 2) if slope_pct is not None else None,
        "slopeDir": slope_dir,
        "status": status,
        "volRatio": round(vol_ratio, 1) if vol_ratio is not None else None,
        "volValue": round(vol_value) if vol_value is not None else None,
        "dayRange": round(day_range, 1) if day_range is not None else None,
        "gap": round(gap, 1) if gap is not None else None,
        "hi52": round(hi52) if hi52 is not None else None,
        "lo52": round(lo52) if lo52 is not None else None,
        "pos52": round(pos52) if pos52 is not None else None,
        "ret1y": round(ret1y, 1) if ret1y is not None else None,
        "rs": rs,
        "scalpScore": sc,
        "scalpGrade": grade,
        "bars": len(closes),
        "_disclaimer": "참고용 지표 — 매매 추천 아님",
    }
