"""trading_rules.py — Expert-grounded rule engine (O'Neil / Minervini based).

READ-ONLY signal engine. It NEVER places an order — it evaluates positions and
entry candidates against a documented ruleset and emits signals that a human (or
plan_order dry-run) can act on. This is the decision core of the trading harness.

Grounding:
- Cut losses at -7~8% (William O'Neil's single most-cited rule).
- Only buy uptrends (Minervini trend template ~ 정배열 + price above moving avgs).
- Never risk more than ~1% of the account on one trade (position sizing from the
  stop distance).
- Sell discipline priority: STOP_LOSS > TREND_EXIT > TAKE_PROFIT > HOLD.
"""
from __future__ import annotations
from typing import Dict, Optional

# ---- Rulesets (pick one; default = user preference) -----------------------
RULESETS = {
    "user":   {"stop_pct": -7.0,  "take_pct": 10.0, "rr": "1.4:1",
               "label": "사용자 선호 (−7% 손절 / +10% 익절)"},
    "oneil":  {"stop_pct": -8.0,  "take_pct": 25.0, "rr": "3.1:1",
               "label": "오닐 원조 CAN SLIM (−8% / +25%)"},
    "tight":  {"stop_pct": -5.0,  "take_pct": 15.0, "rr": "3:1",
               "label": "타이트 (−5% / +15%)"},
}
DEFAULT = "user"

# Entry (Minervini trend-template, simplified to Toss-available indicators)
ENTRY = {
    "require_uptrend": True,       # 정배열 (SMA20>SMA60>SMA120)
    "require_above_ma20": True,    # price > SMA20
    "rsi_min": 40.0, "rsi_max": 72.0,   # momentum band (avoid oversold/blowoff)
    "max_ext_above_ma20_pct": 12.0,     # don't chase >12% extended above SMA20
}
MAX_RISK_PER_TRADE_PCT = 1.0   # never lose >1% of account on one trade

# Pre-existing positions the harness must LEAVE ALONE (no auto stop/take-profit).
# The -7%/+10% discipline applies ONLY to NEW positions the harness opens.
# (Managed separately by the user; the engine only *monitors* these.)
LEGACY_HOLDINGS = {"000660", "MBRX", "AAPL", "TSLA"}


def evaluate_position(pl_pct: Optional[float], last: float, ma20: Optional[float],
                      arrange: Optional[str], ruleset: str = DEFAULT,
                      managed: bool = True) -> Dict:
    """Signal for an OPEN position. pl_pct is the live profit/loss %.
    Priority: STOP_LOSS > TREND_EXIT > TAKE_PROFIT > HOLD.

    ``managed=False`` = a pre-existing/legacy holding the harness must LEAVE
    ALONE: it is only monitored, never given an auto stop/take-profit signal."""
    if not managed:
        return {"signal": "HOLD_LEGACY", "rule": "LEGACY", "action_ko": "기존 보유 유지(규칙 미적용)",
                "reason": f"기존 보유 — 규칙 제외, 모니터만"
                          + (f" (참고 손익 {pl_pct:+.1f}%)" if pl_pct is not None else ""),
                "priority": 9}
    r = RULESETS.get(ruleset, RULESETS[DEFAULT])
    reasons = []
    # 1) hard stop — non-negotiable
    if pl_pct is not None and pl_pct <= r["stop_pct"]:
        sev = "즉시 손절" if pl_pct <= r["stop_pct"] * 2 else "손절"
        return {"signal": "SELL", "rule": "STOP_LOSS", "action_ko": sev,
                "reason": f"손익 {pl_pct:+.1f}% ≤ 손절선 {r['stop_pct']:.0f}%",
                "priority": 1}
    # 2) trend exit — price lost SMA20 (uptrend broken)
    if ma20 and last < ma20:
        reasons.append(f"현재가 {int(last):,} < MA20 {int(ma20):,} (추세 이탈)")
        return {"signal": "REDUCE/SELL", "rule": "TREND_EXIT", "action_ko": "추세이탈 축소·정리",
                "reason": "; ".join(reasons), "priority": 2}
    # 3) take profit
    if pl_pct is not None and pl_pct >= r["take_pct"]:
        return {"signal": "SELL", "rule": "TAKE_PROFIT", "action_ko": "익절",
                "reason": f"손익 {pl_pct:+.1f}% ≥ 익절선 +{r['take_pct']:.0f}%",
                "priority": 3}
    # 4) hold
    return {"signal": "HOLD", "rule": "HOLD", "action_ko": "보유",
            "reason": f"손익 {pl_pct:+.1f}% (손절 {r['stop_pct']:.0f}% ~ 익절 +{r['take_pct']:.0f}% 사이), 추세 유지"
                      if pl_pct is not None else "보유",
            "priority": 4}


def evaluate_entry(ind: Dict) -> Dict:
    """BUY-candidate signal for a watchlist symbol using its indicators dict."""
    last = ind.get("last"); ma20 = ind.get("ma20"); rsi = ind.get("rsi14")
    arrange = ind.get("arrange")
    fails = []
    if ENTRY["require_uptrend"] and arrange != "정배열":
        fails.append(f"추세({arrange}) — 정배열 아님")
    if ENTRY["require_above_ma20"] and ma20 and last is not None and last < ma20:
        fails.append("현재가 MA20 아래")
    if rsi is not None and not (ENTRY["rsi_min"] <= rsi <= ENTRY["rsi_max"]):
        fails.append(f"RSI {rsi} (밴드 {ENTRY['rsi_min']:.0f}~{ENTRY['rsi_max']:.0f} 밖)")
    if ma20 and last is not None:
        ext = (last / ma20 - 1) * 100
        if ext > ENTRY["max_ext_above_ma20_pct"]:
            fails.append(f"MA20 대비 +{ext:.0f}% 과열(추격 금지)")
    if fails:
        return {"signal": "NO_ENTRY", "reason": " / ".join(fails)}
    return {"signal": "BUY_CANDIDATE",
            "reason": f"정배열 + MA20 위 + RSI {rsi} → 진입 후보 (분할·손절 −7% 세팅)"}


def position_size_hint(account_krw: float, entry_price: float,
                       stop_pct: float = -7.0,
                       risk_pct: float = MAX_RISK_PER_TRADE_PCT) -> Dict:
    """How many shares so that hitting the stop loses only risk_pct% of account.
    shares = (account * risk%) / (entry * |stop%|)."""
    risk_krw = account_krw * (risk_pct / 100.0)
    per_share_risk = entry_price * abs(stop_pct) / 100.0
    shares = int(risk_krw / per_share_risk) if per_share_risk > 0 else 0
    return {"maxShares": shares, "riskKRW": int(risk_krw),
            "note": f"손절 {stop_pct:.0f}% 도달 시 계좌의 {risk_pct:.0f}%({int(risk_krw):,}원)만 손실"}


# ---------------------------------------------------------------------------
# Chart-reading upgrades (#21-25)
# ---------------------------------------------------------------------------
def trailing_stop(entry: float, highwater: float, current: float,
                  trail_pct: float = 8.0, init_stop_pct: float = -7.0) -> Dict:
    """#21 — ratchet the stop UP as price makes new highs. The stop starts at
    entry*(1+init_stop%) and, once in profit, trails `trail_pct` below the high.
    Returns the effective stop and whether it is triggered."""
    init_stop = entry * (1 + init_stop_pct / 100.0)
    trail = highwater * (1 - trail_pct / 100.0)
    stop = max(init_stop, trail)                     # never loosen below init
    return {"stop": round(stop, 4), "triggered": current <= stop,
            "locked": stop > entry,                  # stop now above entry = risk-free
            "reason": f"트레일 {trail_pct:.0f}% (고점 {highwater}) → 손절 {round(stop,2)}"}


def support_resistance(candles: list, lookback: int = 40) -> Dict:
    """#22 — recent swing high (resistance) and low (support) over `lookback`
    bars. Candles are canonical (oldest-first) dicts with high/low."""
    rows = candles[-lookback:] if candles else []
    highs = [float(c["high"]) for c in rows if c.get("high") is not None]
    lows = [float(c["low"]) for c in rows if c.get("low") is not None]
    if not highs or not lows:
        return {"support": None, "resistance": None}
    return {"support": round(min(lows), 4), "resistance": round(max(highs), 4),
            "lookback": len(rows)}


def breakout_confirmed(last: float, resistance: float, vol_ratio: float,
                       min_vol_ratio: float = 1.5) -> Dict:
    """#23 — a REAL breakout = price clears recent resistance AND volume expands.
    Filters out the choppy 'sideways 정배열' names (e.g. AMD) that only pass a
    trend filter. vol_ratio = today's volume / average volume."""
    if resistance is None or last is None:
        return {"breakout": False, "reason": "데이터 부족"}
    above = last > resistance
    vol_ok = (vol_ratio or 0) >= min_vol_ratio
    ok = above and vol_ok
    return {"breakout": ok, "aboveResistance": above, "volExpansion": vol_ok,
            "reason": (f"저항 {resistance} 돌파 + 거래량 {vol_ratio:.1f}배" if ok
                       else f"돌파미확인(위={above}, 거래량={vol_ratio}배)")}


def atr_pct(candles: list, n: int = 14) -> Optional[float]:
    """#24 — Average True Range as % of price (volatility). Size down / skip
    names whose ATR% is extreme (too choppy to risk-manage with a -7% stop)."""
    rows = candles[-(n + 1):] if candles else []
    if len(rows) < 2:
        return None
    trs = []
    for i in range(1, len(rows)):
        h = float(rows[i]["high"]); l = float(rows[i]["low"])
        pc = float(rows[i - 1]["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    atr = sum(trs) / len(trs)
    last = float(rows[-1]["close"])
    return round(atr / last * 100.0, 2) if last else None


def time_exit(days_held: int, pl_pct: float, max_days: int = 20,
              min_progress_pct: float = 3.0) -> Dict:
    """#25 — cut a position that has gone nowhere. If held `max_days` and still
    below `min_progress_pct`, the thesis isn't working; free the capital."""
    stale = days_held >= max_days and (pl_pct is None or pl_pct < min_progress_pct)
    return {"exit": stale,
            "reason": (f"{days_held}일 보유·진전 {pl_pct:+.1f}% < +{min_progress_pct:.0f}% → 자본회수"
                       if stale else "진행중")}
