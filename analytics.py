"""analytics.py — Pure alpha-analytics over Toss client response envelopes.

Every function here is **pure**: it takes the dict/list returned by a
``TossClient`` method (or :mod:`mock_data`) and returns a plain dict. There is
NO network access, NO client handle, and NO mode branching — the caller (the MCP
tool layer in ``server.py``) fetches data via the client (mock or live) and
passes the raw envelopes in. This keeps the analytics testable, deterministic,
and identical between mock and live.

Toss envelope shapes consumed (see ``mock_data.py`` / ``toss_client.py``):
  - price:      ``{"symbol", "lastPrice": str, "currency", "timestamp"}``
  - prices:     ``{"prices": [price, ...]}``
  - orderbook:  ``{"symbol", "bids":[{"price":str,"quantity":int}],
                   "asks":[...], "timestamp"}``  (index 0 = best/top-of-book)
  - candles:    ``{"symbol","interval","candles":[{"timestamp","open","high",
                   "low","close","volume"}], "nextBefore"}``  (time-ascending)
  - trades:     ``{"symbol","trades":[{"timestamp","price":str,
                   "quantity":int,"side":"BUY"|"SELL"}]}``  (newest first)
                   NOTE: live ``/trades`` provides neither ``side`` nor
                   ``quantity`` (only ``volume``); ``toss_client._Normalize``
                   maps volume->quantity and *infers* side via the tick rule,
                   stamping ``sideInferred:true`` (mock supplies real sides).
  - price_limits:``{"symbol","upperLimitPrice":str,"lowerLimitPrice":str}``
  - holdings:   ``{"holdings":[{"symbol","name","quantity","averagePrice":str,
                   "currentPrice":str,"evaluationAmount":str,"profitLoss":str,
                   "profitLossRate":str,"cost":str,"currency"}],
                   "summary":{...}}``

Prices arrive as **strings** throughout the Toss BFF; every numeric field is
coerced defensively. All money/quantity outputs are floats unless noted.

Reuses :mod:`indicators` (Wilder RSI, SMA, scalpScore, etc.) so the dashboard
alpha is not re-implemented here.

Supply/demand flow (외국인/기관) is NOT a Toss-provided category — the
``supply_demand_flow`` analyzer therefore tags ``_source`` and never fabricates
numbers in live mode (null is allowed).

Pure stdlib. No pandas/numpy.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import indicators as ind


# ---------------------------------------------------------------------------
# Coercion / small helpers
# ---------------------------------------------------------------------------

def _f(v) -> Optional[float]:
    """Coerce a possibly-string numeric to float, else None."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _round(v: Optional[float], nd: int = 2) -> Optional[float]:
    return round(v, nd) if isinstance(v, (int, float)) else None


def _is_us(symbol: str, currency: Optional[str] = None) -> bool:
    """Heuristic: USD currency or alphabetic ticker -> US market."""
    if currency:
        return currency.upper() == "USD"
    return bool(symbol) and not symbol.isdigit()


def _last_price_from_prices(prices_env: Dict, symbol: str) -> Optional[float]:
    """Pull a symbol's lastPrice out of a ``/prices`` envelope."""
    for p in (prices_env or {}).get("prices", []):
        if p.get("symbol") == symbol:
            return _f(p.get("lastPrice"))
    return None


_DISCLAIMER = "참고용 분석 — 매매 추천 아님. 투자 판단·책임은 계좌주에게 있습니다."


# ===========================================================================
# 1. orderbook_pressure — 호가 매수/매도 불균형비·스프레드·상위호가 벽·체결강도
# ===========================================================================

def orderbook_pressure(orderbook: Dict, trades: Optional[Dict] = None,
                       top_n: int = 5) -> Dict:
    """Order-book microstructure pressure from a ``/orderbook`` envelope.

    Args:
        orderbook: ``{"symbol","bids":[{"price","quantity"}],"asks":[...]}``;
            index 0 is best bid / best ask (top of book).
        trades: optional ``/trades`` envelope to add 체결강도 (buy/sell ratio).
        top_n: number of levels to aggregate for the imbalance / wall analysis.

    Returns:
        {symbol, spread, spreadPct, midPrice, bestBid, bestAsk,
         bidVolumeTopN, askVolumeTopN, imbalance(-1..1), pressure(label),
         bidWall:{price,quantity}, askWall:{price,quantity},
         tradeStrength, _disclaimer}
    """
    symbol = (orderbook or {}).get("symbol")
    bids = (orderbook or {}).get("bids", []) or []
    asks = (orderbook or {}).get("asks", []) or []

    best_bid = _f(bids[0].get("price")) if bids else None
    best_ask = _f(asks[0].get("price")) if asks else None
    spread = None
    spread_pct = None
    mid = None
    if best_bid is not None and best_ask is not None:
        spread = best_ask - best_bid
        mid = (best_bid + best_ask) / 2.0
        spread_pct = (spread / mid * 100.0) if mid else None

    def _sum_qty(levels):
        return sum((_f(l.get("quantity")) or 0.0) for l in levels[:top_n])

    bid_vol = _sum_qty(bids)
    ask_vol = _sum_qty(asks)
    total = bid_vol + ask_vol
    imbalance = ((bid_vol - ask_vol) / total) if total else None  # -1..1

    if imbalance is None:
        pressure = "정보부족"
    elif imbalance >= 0.33:
        pressure = "매수우위"
    elif imbalance <= -0.33:
        pressure = "매도우위"
    else:
        pressure = "균형"

    def _wall(levels):
        best = None
        for l in levels[:max(top_n, 10)]:
            q = _f(l.get("quantity")) or 0.0
            if best is None or q > best[1]:
                best = (_f(l.get("price")), q)
        return {"price": best[0], "quantity": best[1]} if best else None

    strength = None
    if trades:
        strength = ind.trade_strength(trades.get("trades", []))

    return {
        "symbol": symbol,
        "spread": _round(spread, 4),
        "spreadPct": _round(spread_pct, 4),
        "midPrice": _round(mid, 4),
        "bestBid": best_bid,
        "bestAsk": best_ask,
        "bidVolumeTopN": bid_vol,
        "askVolumeTopN": ask_vol,
        "topN": top_n,
        "imbalance": _round(imbalance, 4),
        "pressure": pressure,
        "bidWall": _wall(bids),
        "askWall": _wall(asks),
        "tradeStrength": strength,
        "_disclaimer": _DISCLAIMER,
    }


# ===========================================================================
# 2. intraday_vwap — 당일 1m캔들 → VWAP·vwapGap%·레인지내 위치
# ===========================================================================

def _aggregate_1m_to_5m(candles: List[Dict]) -> List[Dict]:
    """Aggregate time-ascending 1m candles into 5m buckets.

    open=first open, high=max high, low=min low, close=last close,
    volume=sum. Partial trailing bucket (intraday tail) is kept as-is.
    """
    out: List[Dict] = []
    for i in range(0, len(candles), 5):
        chunk = candles[i:i + 5]
        if not chunk:
            continue
        opens = [_f(c.get("open")) for c in chunk]
        highs = [_f(c.get("high")) for c in chunk]
        lows = [_f(c.get("low")) for c in chunk]
        closes = [_f(c.get("close")) for c in chunk]
        vols = [_f(c.get("volume")) or 0.0 for c in chunk]
        # close is required (mirror indicators invariant); skip if absent.
        closes_ok = [c for c in closes if c is not None]
        if not closes_ok:
            continue
        first_open = next((o for o in opens if o is not None), closes_ok[0])
        highs_ok = [h for h in highs if h is not None] or closes_ok
        lows_ok = [l for l in lows if l is not None] or closes_ok
        out.append({
            "timestamp": chunk[0].get("timestamp"),
            "open": first_open,
            "high": max(highs_ok),
            "low": min(lows_ok),
            "close": closes_ok[-1],
            "volume": sum(vols),
        })
    return out


def intraday_vwap(candles: Dict, prev_close: Optional[float] = None,
                  current_price: Optional[float] = None,
                  aggregate_5m: bool = True) -> Dict:
    """Intraday VWAP from a ``/candles(interval=1m)`` envelope.

    Faithful port of stock-dashboard ``fetch_intraday()`` VWAP logic (server.py
    319-345): per bar 대표가격 ``tp=(high+low+close)/3``, cumulative
    ``cum_pv += tp*vol`` / ``cum_v += vol`` (vol 0 -> use close), ``vwapNow`` =
    last vwap, ``vwapGap = (last - vwapNow)/vwapNow*100``,
    ``rangePos = (last - dayLow)/(dayHigh - dayLow)*100``.

    Args:
        candles: ``/candles`` envelope with 1m bars (time-ascending).
        prev_close: previous session close (from ``/prices`` or prior 1d candle).
        current_price: live last price; falls back to last 1m close.
        aggregate_5m: aggregate 1m -> 5m before computing (skill §3 default True).

    Returns:
        {open, high, low, last, prevClose, vwapNow, vwapGap, rangePos, bars,
         vwapSeries(tail), _disclaimer}
    """
    bars_raw = (candles or {}).get("candles", []) or []
    bars = _aggregate_1m_to_5m(bars_raw) if aggregate_5m else [
        {"timestamp": c.get("timestamp"), "open": _f(c.get("open")),
         "high": _f(c.get("high")), "low": _f(c.get("low")),
         "close": _f(c.get("close")), "volume": _f(c.get("volume")) or 0.0}
        for c in bars_raw if _f(c.get("close")) is not None
    ]
    if len(bars) < 1:
        return {"error": {"code": "INVALID_PARAM",
                          "message_ko": "분봉 데이터 부족"}}

    closes: List[float] = []
    highs: List[float] = []
    lows: List[float] = []
    vwap: List[float] = []
    cum_pv = cum_v = 0.0
    for b in bars:
        c = b["close"]
        if c is None:
            continue
        h = b["high"] if b["high"] is not None else c
        lo = b["low"] if b["low"] is not None else c
        vol = b["volume"] or 0.0
        tp = (h + lo + c) / 3.0
        cum_pv += tp * vol
        cum_v += vol
        closes.append(c)
        highs.append(h)
        lows.append(lo)
        vwap.append(cum_pv / cum_v if cum_v else c)

    if not closes:
        return {"error": {"code": "INVALID_PARAM",
                          "message_ko": "분봉 데이터 부족"}}

    last = current_price if current_price is not None else closes[-1]
    day_open = closes[0]
    day_hi, day_lo = max(highs), min(lows)
    vwap_now = vwap[-1] if vwap else last
    vwap_gap = ((last - vwap_now) / vwap_now * 100.0) if vwap_now else None
    rng_pos = ((last - day_lo) / (day_hi - day_lo) * 100.0
               if day_hi > day_lo else None)

    return {
        "open": _round(day_open, 4),
        "high": _round(day_hi, 4),
        "low": _round(day_lo, 4),
        "last": _round(last, 4),
        "prevClose": _round(prev_close, 4),
        "vwapNow": _round(vwap_now, 4),
        "vwapGap": _round(vwap_gap, 2),
        "rangePos": round(rng_pos) if rng_pos is not None else None,
        "bars": len(closes),
        "aggregated5m": aggregate_5m,
        "vwapSeries": [_round(v, 2) for v in vwap[-12:]],
        "_disclaimer": _DISCLAIMER,
    }


# ===========================================================================
# 3. recent_trades_tape — 체결강도·대량체결 표식·틱흐름
# ===========================================================================

def recent_trades_tape(trades: Dict, big_trade_mult: float = 3.0) -> Dict:
    """Time-and-sales tape analytics from a ``/trades`` envelope.

    Args:
        trades: ``{"trades":[{"price","quantity","side","timestamp"}]}``
            (newest first per Toss).
        big_trade_mult: a trade is flagged 대량체결 if its quantity exceeds
            ``big_trade_mult * mean(quantity)``.

    Returns:
        {symbol, count, buyVolume, sellVolume, tradeStrength,
         buyRatio, tickFlow(label), avgQty, bigTrades:[...], lastPrice,
         priceDrift, _disclaimer}
    """
    symbol = (trades or {}).get("symbol")
    rows = (trades or {}).get("trades", []) or []
    if not rows:
        return {"symbol": symbol, "count": 0, "buyVolume": 0.0,
                "sellVolume": 0.0, "tradeStrength": None, "buyRatio": None,
                "tickFlow": "정보부족", "avgQty": None, "bigTrades": [],
                "lastPrice": None, "priceDrift": None,
                "_disclaimer": _DISCLAIMER}

    buy = sell = 0.0
    qtys: List[float] = []
    up_ticks = down_ticks = 0
    prev_price = None
    # rows are newest-first; iterate oldest-first for tick direction.
    for t in reversed(rows):
        q = _f(t.get("quantity")) or 0.0
        p = _f(t.get("price"))
        qtys.append(q)
        if t.get("side") == "BUY":
            buy += q
        elif t.get("side") == "SELL":
            sell += q
        if prev_price is not None and p is not None:
            if p > prev_price:
                up_ticks += 1
            elif p < prev_price:
                down_ticks += 1
        if p is not None:
            prev_price = p

    strength = ind.trade_strength(rows)
    total_vol = buy + sell
    buy_ratio = (buy / total_vol) if total_vol else None
    avg_qty = (sum(qtys) / len(qtys)) if qtys else None

    big = []
    if avg_qty:
        thresh = big_trade_mult * avg_qty
        for t in rows:
            q = _f(t.get("quantity")) or 0.0
            if q >= thresh:
                big.append({"price": _f(t.get("price")), "quantity": q,
                            "side": t.get("side"),
                            "timestamp": t.get("timestamp")})

    if up_ticks + down_ticks == 0:
        tick_flow = "보합"
    elif up_ticks > down_ticks * 1.2:
        tick_flow = "상승틱우세"
    elif down_ticks > up_ticks * 1.2:
        tick_flow = "하락틱우세"
    else:
        tick_flow = "혼조"

    newest = _f(rows[0].get("price"))
    oldest = _f(rows[-1].get("price"))
    drift = None
    if newest is not None and oldest:
        drift = (newest - oldest) / oldest * 100.0

    return {
        "symbol": symbol,
        "count": len(rows),
        "buyVolume": buy,
        "sellVolume": sell,
        "tradeStrength": strength,
        "buyRatio": _round(buy_ratio, 3),
        "tickFlow": tick_flow,
        "upTicks": up_ticks,
        "downTicks": down_ticks,
        "avgQty": _round(avg_qty, 1),
        "bigTrades": big[:10],
        "lastPrice": newest,
        "priceDrift": _round(drift, 3),
        "_disclaimer": _DISCLAIMER,
    }


# ===========================================================================
# 4. price_limit_proximity — 상/하한 근접도%·잔여폭 (KRX 전용)
# ===========================================================================

def price_limit_proximity(price_limits: Dict, current_price: float,
                          symbol: Optional[str] = None,
                          currency: Optional[str] = None) -> Dict:
    """Distance to daily upper/lower price limits from ``/price-limits``.

    KRX cash equities have a +/-30% daily limit; US has no fixed daily limit,
    so the US branch is explicitly labelled and proximity is informational only.

    Args:
        price_limits: ``{"upperLimitPrice":str,"lowerLimitPrice":str,...}``.
        current_price: live price (numeric).
        symbol / currency: used to decide KRX vs US labelling.

    Returns:
        {symbol, market, currentPrice, upperLimit, lowerLimit,
         upRoomPct, downRoomPct, proximity(label), isUS, note, _disclaimer}
    """
    sym = symbol or (price_limits or {}).get("symbol")
    upper = _f((price_limits or {}).get("upperLimitPrice"))
    lower = _f((price_limits or {}).get("lowerLimitPrice"))
    price = _f(current_price)
    is_us = _is_us(sym or "", currency)

    up_room = None
    down_room = None
    if price and upper:
        up_room = (upper - price) / price * 100.0      # % headroom to ceiling
    if price and lower:
        down_room = (price - lower) / price * 100.0    # % cushion above floor

    # Proximity label (KRX semantics: near ceiling/floor = caution).
    proximity = "중앙권"
    if up_room is not None and up_room <= 2.0:
        proximity = "상한근접"
    elif down_room is not None and down_room <= 2.0:
        proximity = "하한근접"
    elif up_room is not None and up_room <= 8.0:
        proximity = "상단권"
    elif down_room is not None and down_room <= 8.0:
        proximity = "하단권"

    note = ("미국주는 일일 가격제한(상/하한)이 KRX와 달라 고정 제한이 없습니다 — "
            "근접도는 참고용 밴드입니다." if is_us
            else "KRX 일일 가격제한(±30%) 기준 근접도입니다.")

    return {
        "symbol": sym,
        "market": "US" if is_us else "KR",
        "currentPrice": price,
        "upperLimit": upper,
        "lowerLimit": lower,
        "upRoomPct": _round(up_room, 2),
        "downRoomPct": _round(down_room, 2),
        "proximity": proximity,
        "isUS": is_us,
        "note": note,
        "_disclaimer": _DISCLAIMER,
    }


# ===========================================================================
# 5. scalp_score (다건; indicators 재사용)
# ===========================================================================

def scalp_score_one(candles: Dict, symbol: str,
                    current_price: Optional[float] = None,
                    trades: Optional[Dict] = None) -> Dict:
    """Single-symbol 단타 적합도 — thin wrapper that reuses indicators.compute().

    Args:
        candles: daily ``/candles`` envelope (>=21 bars recommended).
        symbol: the symbol (for labelling).
        current_price: live price (from ``/prices``); falls back to last close.
        trades: optional ``/trades`` envelope for 체결강도 context.

    Returns:
        {symbol, scalpScore, scalpGrade, reasons:[...], volValue, dayRange,
         volRatio, changePct, tradeStrength, _disclaimer}
    """
    full = ind.compute(candles.get("candles", []) if candles else [],
                       (trades or {}).get("trades") if trades else None,
                       current_price=current_price)
    reasons = []
    if full.get("volValue") is not None:
        reasons.append(f"거래대금 {full['volValue']}억")
    if full.get("dayRange") is not None:
        reasons.append(f"일변동폭 {full['dayRange']}%")
    if full.get("volRatio") is not None:
        reasons.append(f"거래량 {full['volRatio']}배")
    if full.get("changePct") is not None:
        reasons.append(f"당일 {full['changePct']}%")
    return {
        "symbol": symbol,
        "scalpScore": full.get("scalpScore"),
        "scalpGrade": full.get("scalpGrade"),
        "reasons": reasons,
        "volValue": full.get("volValue"),
        "dayRange": full.get("dayRange"),
        "volRatio": full.get("volRatio"),
        "changePct": full.get("changePct"),
        "tradeStrength": full.get("tradeStrength"),
        "_disclaimer": _DISCLAIMER,
    }


def scalp_score(symbol_candles: Dict[str, Dict],
                symbol_prices: Optional[Dict[str, float]] = None,
                symbol_trades: Optional[Dict[str, Dict]] = None) -> Dict:
    """Multi-symbol 단타 적합도 ranking.

    Args:
        symbol_candles: ``{symbol: /candles envelope}`` (caller fetched each).
        symbol_prices: ``{symbol: current_price}`` (optional live prices).
        symbol_trades: ``{symbol: /trades envelope}`` (optional).

    Returns:
        {results: [scalp_score_one(...), ...] sorted desc by scalpScore,
         count, _disclaimer}
    """
    symbol_prices = symbol_prices or {}
    symbol_trades = symbol_trades or {}
    results = []
    for sym, cand in (symbol_candles or {}).items():
        results.append(scalp_score_one(
            cand, sym,
            current_price=symbol_prices.get(sym),
            trades=symbol_trades.get(sym)))
    results.sort(key=lambda r: (r.get("scalpScore") or -1), reverse=True)
    return {"results": results, "count": len(results),
            "_disclaimer": _DISCLAIMER}


# ===========================================================================
# 6. supply_demand_flow — 외국인/기관 순매수 (토스 미제공 → _source 표기)
# ===========================================================================

def supply_demand_flow(symbol: str, naver_flow: Optional[Dict] = None,
                       mode: str = "mock") -> Dict:
    """Foreign/institutional net-buy flow — NOT a Toss category.

    Toss Open API does not expose 외국인/기관 수급. This analyzer therefore:
      - In live mode, if no auxiliary (Naver) source is supplied, returns nulls
        with ``_source:"unavailable"`` and NEVER fabricates numbers.
      - If a Naver-shaped flow dict is supplied (from stock-dashboard's
        ``fetch_flow``), it is passed through with ``_source:"naver"``.
      - In mock mode with no source, a clearly-labelled synthetic placeholder
        may be returned by the caller; this pure function only normalizes/labels.

    Args:
        symbol: the symbol.
        naver_flow: optional dict shaped like stock-dashboard ``fetch_flow``:
            ``{date, foreignVal, instVal, indivVal, foreignShares, instShares,
            foreignHold}`` (금액 억원).
        mode: "mock" | "live" (controls fabrication policy in callers; recorded
            here for transparency).

    Returns:
        {symbol, date, foreignVal, instVal, indivVal, foreignShares,
         instShares, foreignHold, trend, _source, _krxOnly, note, _disclaimer}
    """
    is_us = _is_us(symbol)
    if is_us:
        return {
            "symbol": symbol,
            "date": None, "foreignVal": None, "instVal": None,
            "indivVal": None, "foreignShares": None, "instShares": None,
            "foreignHold": None, "trend": None,
            "_source": "unavailable", "_krxOnly": True,
            "note": "외국인/기관 수급은 국내(KRX) 전용 — 미국 종목 미지원",
            "_disclaimer": _DISCLAIMER,
        }

    if not naver_flow:
        return {
            "symbol": symbol,
            "date": None, "foreignVal": None, "instVal": None,
            "indivVal": None, "foreignShares": None, "instShares": None,
            "foreignHold": None, "trend": None,
            "_source": "unavailable", "_krxOnly": True,
            "note": ("외국인/기관 수급은 토스 Open API 미제공 — "
                     "stock-dashboard(Naver) 보조 소스 필요. live에서 추측 금지."),
            "_disclaimer": _DISCLAIMER,
        }

    f_val = _f(naver_flow.get("foreignVal"))
    i_val = _f(naver_flow.get("instVal"))
    # Trend label from the sign of foreign + institutional net buying (억원).
    trend = None
    if f_val is not None and i_val is not None:
        net = f_val + i_val
        if net > 0:
            trend = "기관·외국인 순매수"
        elif net < 0:
            trend = "기관·외국인 순매도"
        else:
            trend = "중립"

    return {
        "symbol": symbol,
        "date": naver_flow.get("date"),
        "foreignVal": f_val,
        "instVal": i_val,
        "indivVal": _f(naver_flow.get("indivVal")),
        "foreignShares": naver_flow.get("foreignShares"),
        "instShares": naver_flow.get("instShares"),
        "foreignHold": _f(naver_flow.get("foreignHold")),
        "trend": trend,
        "_source": "naver",
        "_krxOnly": True,
        "note": "외국인/기관 수급은 네이버 보조 소스 — 토스 미제공.",
        "_disclaimer": _DISCLAIMER,
    }


# ===========================================================================
# 7. rank_watchlist — watchlist 집계 → TOP N + 한줄 브리핑
# ===========================================================================

def rank_watchlist(symbol_candles: Dict[str, Dict],
                   symbol_meta: Optional[Dict[str, Dict]] = None,
                   symbol_prices: Optional[Dict[str, float]] = None,
                   top_n: int = 5, sort_by: str = "scalpScore") -> Dict:
    """Aggregate a watchlist into a TOP-N board + one-line briefings.

    Args:
        symbol_candles: ``{symbol: /candles envelope}`` (caller fetched each).
        symbol_meta: ``{symbol: {"name":..., "currency":...}}`` (optional).
        symbol_prices: ``{symbol: current_price}`` (optional live prices).
        top_n: number of rows to surface.
        sort_by: ranking key — ``scalpScore`` | ``changePct`` | ``volValue`` |
            ``rsi`` | ``volRatio``.

    Returns:
        {ranked:[{symbol,name,scalpScore,scalpGrade,changePct,rsi,status,
                  arrange,volValue,briefing}], topN, sortBy, count, _disclaimer}
    """
    symbol_meta = symbol_meta or {}
    symbol_prices = symbol_prices or {}
    rows = []
    for sym, cand in (symbol_candles or {}).items():
        full = ind.compute(cand.get("candles", []) if cand else [],
                           current_price=symbol_prices.get(sym))
        meta = symbol_meta.get(sym, {})
        name = meta.get("name", sym)
        rows.append({
            "symbol": sym,
            "name": name,
            "scalpScore": full.get("scalpScore"),
            "scalpGrade": full.get("scalpGrade"),
            "changePct": full.get("changePct"),
            "rsi": full.get("rsi14"),
            "status": full.get("status"),
            "arrange": full.get("arrange"),
            "volValue": full.get("volValue"),
            "volRatio": full.get("volRatio"),
            "briefing": _briefing(name, full),
        })

    valid_keys = {"scalpScore", "changePct", "volValue", "rsi", "volRatio"}
    key = sort_by if sort_by in valid_keys else "scalpScore"
    key_field = "rsi" if key == "rsi" else key
    rows.sort(key=lambda r: (r.get(key_field) if r.get(key_field) is not None
                             else float("-inf")), reverse=True)
    return {
        "ranked": rows[:top_n],
        "topN": top_n,
        "sortBy": key,
        "count": len(rows),
        "_disclaimer": _DISCLAIMER,
    }


def _briefing(name: str, full: Dict) -> str:
    """One-line natural-language briefing from a compute() result."""
    parts = [name]
    status = full.get("status")
    if status and status != "중립":
        parts.append(status)
    grade = full.get("scalpGrade")
    score = full.get("scalpScore")
    if grade is not None:
        parts.append(f"단타 {grade}({score})")
    chg = full.get("changePct")
    if chg is not None:
        parts.append(f"{'+' if chg >= 0 else ''}{chg}%")
    rsi_v = full.get("rsi14")
    if rsi_v is not None:
        parts.append(f"RSI {rsi_v}")
    arrange = full.get("arrange")
    if arrange:
        parts.append(arrange)
    return " · ".join(str(p) for p in parts)


# ===========================================================================
# 8. portfolio_risk_xray — 집중도(허핀달)·최대비중·시장/통화/현금 비중·플래그
# ===========================================================================

def portfolio_risk_xray(holdings: Dict,
                        buying_power_krw: Optional[float] = None,
                        usdkrw: float = 1380.0,
                        max_single_weight: float = 0.30,
                        hhi_warn: float = 0.25) -> Dict:
    """Concentration / cash / currency risk x-ray from a ``/holdings`` envelope.

    All positions are normalized to KRW (US positions via ``usdkrw``) so weights
    are comparable across markets. Concentration uses the Herfindahl-Hirschman
    Index (HHI = sum of squared weights, 0..1; higher = more concentrated).

    Args:
        holdings: ``/holdings`` envelope (``holdings`` list with
            ``evaluationAmount``, ``currency`` per row).
        buying_power_krw: optional cash (KRW) to compute 현금비중. If None, cash
            is treated as 0 (cash weight null).
        usdkrw: USD->KRW rate used for normalization (must be supplied/verified;
            stated in output for transparency).
        max_single_weight: single-name weight that triggers a 집중 flag.
        hhi_warn: HHI threshold that triggers a 분산부족 flag.

    Returns:
        {totalEquityKRW, totalAssetKRW, cashKRW, cashWeight, positions:[...],
         hhi, maxSingle:{symbol,weight}, marketWeights:{KR,US},
         currencyWeights, flags:[...], usdkrw, _disclaimer}
    """
    rows = (holdings or {}).get("holdings", []) or []
    positions = []
    total_eq = 0.0
    market_krw = {"KR": 0.0, "US": 0.0}
    for r in rows:
        cur = (r.get("currency") or "").upper()
        eval_amt = _f(r.get("evaluationAmount")) or 0.0
        krw_val = eval_amt * usdkrw if cur == "USD" else eval_amt
        sym = r.get("symbol")
        is_us = cur == "USD" or _is_us(sym or "")
        market_krw["US" if is_us else "KR"] += krw_val
        positions.append({
            "symbol": sym, "name": r.get("name"),
            "currency": cur or ("USD" if is_us else "KRW"),
            "evalKRW": krw_val, "isUS": is_us,
            "profitLoss": _f(r.get("profitLoss")),
        })
        total_eq += krw_val

    cash = _f(buying_power_krw)
    total_asset = total_eq + (cash or 0.0)

    # Weights over total equity (positions) for HHI/concentration.
    hhi = 0.0
    max_single = {"symbol": None, "weight": None}
    for p in positions:
        w = (p["evalKRW"] / total_eq) if total_eq else 0.0
        p["weight"] = round(w, 4)
        hhi += w * w
        if max_single["weight"] is None or w > max_single["weight"]:
            max_single = {"symbol": p["symbol"], "weight": round(w, 4)}

    cash_weight = (cash / total_asset) if (cash is not None and total_asset) else None
    market_weights = {
        m: round(v / total_eq, 4) if total_eq else 0.0
        for m, v in market_krw.items()
    }
    currency_weights = {
        "KRW": market_weights["KR"], "USD": market_weights["US"],
    }

    flags = []
    if max_single["weight"] is not None and max_single["weight"] >= max_single_weight:
        flags.append({
            "code": "SINGLE_CONCENTRATION",
            "message_ko": (f"단일종목({max_single['symbol']}) 비중 "
                           f"{round(max_single['weight']*100,1)}% — 집중 위험"),
        })
    if hhi >= hhi_warn and len(positions) > 0:
        flags.append({
            "code": "LOW_DIVERSIFICATION",
            "message_ko": f"집중도(HHI) {round(hhi,3)} — 분산 부족",
        })
    if cash_weight is not None and cash_weight < 0.05:
        flags.append({
            "code": "LOW_CASH",
            "message_ko": f"현금비중 {round(cash_weight*100,1)}% — 현금 여력 낮음",
        })
    if currency_weights["USD"] >= 0.60:
        flags.append({
            "code": "FX_CONCENTRATION",
            "message_ko": f"USD 비중 {round(currency_weights['USD']*100,1)}% — 환위험 집중",
        })

    positions.sort(key=lambda p: p["evalKRW"], reverse=True)

    return {
        "totalEquityKRW": round(total_eq),
        "totalAssetKRW": round(total_asset),
        "cashKRW": round(cash) if cash is not None else None,
        "cashWeight": round(cash_weight, 4) if cash_weight is not None else None,
        "positions": positions,
        "hhi": round(hhi, 4),
        "maxSingle": max_single,
        "marketWeights": market_weights,
        "currencyWeights": currency_weights,
        "flags": flags,
        "usdkrw": usdkrw,
        "note": "US 평가액은 표기 환율로 KRW 환산 — 환율은 별도 검증 필요.",
        "_disclaimer": _DISCLAIMER,
    }


# ===========================================================================
# 9. pnl_attribution — 종목별 손익기여 TOP·귀속요약
# ===========================================================================

def pnl_attribution(holdings: Dict, usdkrw: float = 1380.0,
                    top_n: int = 5) -> Dict:
    """Per-symbol P&L attribution from a ``/holdings`` envelope.

    Each position's profitLoss is normalized to KRW; contribution = its KRW P&L
    as a share of total KRW P&L (signed). Surfaces top winners and losers.

    Args:
        holdings: ``/holdings`` envelope.
        usdkrw: USD->KRW for normalization (stated in output).
        top_n: number of contributors to surface on each side.

    Returns:
        {totalPnLKRW, contributors:[{symbol,name,pnlKRW,contribution,
         profitLossRate}], topWinners:[...], topLosers:[...], summary,
         usdkrw, _disclaimer}
    """
    rows = (holdings or {}).get("holdings", []) or []
    contributors = []
    total_pnl = 0.0
    for r in rows:
        cur = (r.get("currency") or "").upper()
        pnl = _f(r.get("profitLoss")) or 0.0
        pnl_krw = pnl * usdkrw if cur == "USD" else pnl
        contributors.append({
            "symbol": r.get("symbol"),
            "name": r.get("name"),
            "pnlKRW": round(pnl_krw),
            "profitLossRate": _f(r.get("profitLossRate")),
            "currency": cur or "KRW",
        })
        total_pnl += pnl_krw

    for c in contributors:
        c["contribution"] = (round(c["pnlKRW"] / total_pnl, 4)
                             if total_pnl else None)

    by_pnl = sorted(contributors, key=lambda c: c["pnlKRW"], reverse=True)
    winners = [c for c in by_pnl if c["pnlKRW"] > 0][:top_n]
    losers = [c for c in reversed(by_pnl) if c["pnlKRW"] < 0][:top_n]

    top_win = winners[0]["name"] if winners else None
    top_lose = losers[0]["name"] if losers else None
    summary_parts = [f"총손익 {round(total_pnl):,}원"]
    if top_win:
        summary_parts.append(f"기여1위 {top_win}")
    if top_lose:
        summary_parts.append(f"부진1위 {top_lose}")
    summary = " · ".join(summary_parts)

    return {
        "totalPnLKRW": round(total_pnl),
        "contributors": by_pnl,
        "topWinners": winners,
        "topLosers": losers,
        "summary": summary,
        "usdkrw": usdkrw,
        "_disclaimer": _DISCLAIMER,
    }


# ===========================================================================
# 10. impact_of_order — 가상 주문 → 집중도/현금/손익 변화 (실행 없음)
# ===========================================================================

def impact_of_order(holdings: Dict, symbol: str, side: str, quantity: float,
                    price: float, currency: str,
                    buying_power_krw: Optional[float] = None,
                    usdkrw: float = 1380.0,
                    symbol_name: Optional[str] = None) -> Dict:
    """What-if: how a *hypothetical* order changes concentration / cash / P&L.

    Executes NOTHING — it simulates applying the order to the holdings snapshot
    and re-runs :func:`portfolio_risk_xray` on a synthetic post-trade portfolio.
    Validates currency/market coherence (KRX symbol + USD, or US symbol + KRW =
    hard fail) per safety invariant 12.

    Args:
        holdings: current ``/holdings`` envelope.
        symbol, side ("BUY"|"SELL"), quantity, price: the hypothetical order.
        currency: "KRW" | "USD" — must match the symbol's market.
        buying_power_krw: optional current cash (KRW) for cash-weight delta.
        usdkrw: USD->KRW for normalization.
        symbol_name: display name for the simulated position.

    Returns:
        On success: {ok:True, data:{before, after, delta:{...}, order, _mock?}}
        wrapped at tool level. Here returns the inner data dict; on validation
        failure returns ``{"error":{"code","message_ko"}}``.
    """
    side = (side or "").upper()
    currency = (currency or "").upper()
    is_us = _is_us(symbol, currency)

    # --- safety invariant 12: currency/market coherence ---
    if currency not in {"KRW", "USD"}:
        return {"error": {"code": "INVALID_PARAM",
                          "message_ko": "currency는 KRW 또는 USD여야 합니다."}}
    sym_is_digit = bool(symbol) and symbol.isdigit()
    if sym_is_digit and currency == "USD":
        return {"error": {"code": "CURRENCY_MISMATCH",
                          "message_ko": "국내(KRX) 종목에 USD 통화는 불일치입니다."}}
    if (not sym_is_digit) and currency == "KRW" and symbol.isalpha():
        return {"error": {"code": "CURRENCY_MISMATCH",
                          "message_ko": "미국 종목에 KRW 통화는 불일치입니다."}}
    if side not in {"BUY", "SELL"}:
        return {"error": {"code": "INVALID_PARAM",
                          "message_ko": "side는 BUY 또는 SELL이어야 합니다."}}
    qty = _f(quantity)
    px = _f(price)
    if qty is None or qty <= 0 or px is None or px <= 0:
        return {"error": {"code": "INVALID_PARAM",
                          "message_ko": "quantity·price는 0보다 커야 합니다."}}
    if sym_is_digit and qty != int(qty):
        return {"error": {"code": "INVALID_PARAM",
                          "message_ko": "국내(KRX) 수량은 정수여야 합니다."}}

    notional = qty * px                       # in order currency
    notional_krw = notional * usdkrw if currency == "USD" else notional

    # --- before snapshot ---
    before = portfolio_risk_xray(holdings, buying_power_krw=buying_power_krw,
                                 usdkrw=usdkrw)

    # --- build synthetic post-trade holdings ---
    rows = [dict(r) for r in (holdings or {}).get("holdings", []) or []]
    found = None
    for r in rows:
        if r.get("symbol") == symbol:
            found = r
            break

    sign = 1.0 if side == "BUY" else -1.0
    if found is not None:
        cur_eval = _f(found.get("evaluationAmount")) or 0.0
        # eval is in the position's own currency; adjust by qty*px in that ccy.
        new_eval = cur_eval + sign * notional
        if new_eval < 0:
            new_eval = 0.0
        found["evaluationAmount"] = f"{new_eval}"
        found.setdefault("currency", currency)
    elif side == "BUY":
        rows.append({
            "symbol": symbol,
            "name": symbol_name or symbol,
            "quantity": qty,
            "averagePrice": f"{px}",
            "currentPrice": f"{px}",
            "evaluationAmount": f"{notional}",
            "profitLoss": "0",
            "profitLossRate": "0",
            "currency": currency,
        })
    else:
        return {"error": {"code": "INVALID_PARAM",
                          "message_ko": "보유하지 않은 종목은 매도 시뮬레이션 불가합니다."}}

    # cash delta: BUY consumes cash, SELL adds cash (KRW-normalized).
    new_cash = None
    if buying_power_krw is not None:
        new_cash = _f(buying_power_krw) - sign * notional_krw

    synthetic = {"holdings": rows}
    after = portfolio_risk_xray(synthetic, buying_power_krw=new_cash,
                                usdkrw=usdkrw)

    def _d(a, b):
        if a is None or b is None:
            return None
        return round(a - b, 4)

    delta = {
        "hhi": _d(after["hhi"], before["hhi"]),
        "maxSingleWeight": _d(after["maxSingle"]["weight"],
                              before["maxSingle"]["weight"]),
        "cashWeight": _d(after["cashWeight"], before["cashWeight"]),
        "usdWeight": _d(after["currencyWeights"]["USD"],
                        before["currencyWeights"]["USD"]),
        "totalEquityKRW": _d(after["totalEquityKRW"], before["totalEquityKRW"]),
    }

    return {
        "order": {"symbol": symbol, "side": side, "quantity": qty,
                  "price": px, "currency": currency,
                  "notional": round(notional, 2),
                  "notionalKRW": round(notional_krw)},
        "before": before,
        "after": after,
        "delta": delta,
        "newFlags": [f for f in after["flags"]
                     if f["code"] not in {b["code"] for b in before["flags"]}],
        "executed": False,
        "usdkrw": usdkrw,
        "note": "what-if 시뮬레이션 — 실제 주문 실행 없음.",
        "_disclaimer": _DISCLAIMER,
    }
