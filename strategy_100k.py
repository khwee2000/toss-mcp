"""strategy_100k.py — a careful, small-capital (₩100,000) momentum strategy.

Design goals (honest):
- ₩100k is a LEARNING-sized account. Fees/tax/FX spread are a real drag, so we
  keep it to ONE position at a time and trade infrequently (quality > quantity).
- KR large-caps cost more than ₩100k for a single share, so the investable
  instrument is US stocks via Toss AMOUNT orders (fractional, USD).
- Discipline is the edge, not prediction: only enter confirmed uptrends, and
  every entry ships with a hard -7% stop (William O'Neil's rule).

Rules
-----
Universe : liquid US momentum leaders (fractional-friendly).
Entry    : 정배열(MA20>MA60>MA120) + price>MA20 + RSI 40~72 + not >12% extended.
Sizing   : deploy the full ~₩100k into the single best-ranked candidate
           (splitting ₩100k across names lets fees dominate).
Exit     : STOP -7%  >  TREND_EXIT(close<MA20)  >  TAKE_PROFIT +10%.
Cadence  : evaluate daily (or via the harness loop).
Cash-wait: if nothing qualifies, HOLD CASH — forcing a trade in a downtrend is
           how small accounts die. "No trade" is a valid, disciplined output.

This module is READ-ONLY: it scans, ranks, and produces a DRY-RUN plan. It never
places an order. Going live requires: fund the account, TOSS_ALLOW_LIVE_ORDERS=1,
and your explicit confirm_phrase on the preview.
"""
from __future__ import annotations
from typing import Dict, List

import trading_rules as TR

CAPITAL_KRW = 100_000
RULESET = "user"            # -7% / +10%  (switch to "oneil" for -8/+25)
MAX_POSITIONS = 3           # portfolio, not a single name

# Regime-aware deployment: in a risk-off tape (index proxies below MA20) we
# deploy LESS and hold cash; in a healthy tape we deploy more.
INDEX_PROXIES = {"069500": "코스피(KODEX200)", "229200": "코스닥150",
                 "360750": "S&P500(TIGER)"}
DEPLOY = {"risk_on": 0.90, "neutral": 0.75, "risk_off": 0.60}

# Partial take-profit ladder (matches "중간 익절"): scale out, let a runner go.
TP_LADDER = [(5.0, 0.5),    # +5%  -> sell 50%
             (10.0, 0.5)]   # +10% -> sell the rest (or trail on MA20 break)


def assess_regime(snap) -> Dict:
    """snap(sym)->indicators. Regime from how many index proxies hold MA20."""
    holds = tot = 0
    detail = []
    for sym, name in INDEX_PROXIES.items():
        r = snap(sym)
        if not r or r.get("last") is None or r.get("ma20") is None:
            continue
        tot += 1
        above = r["last"] > r["ma20"]
        holds += 1 if above else 0
        detail.append(f"{name}:{'▲' if above else '▼'}MA20(RSI{r.get('rsi14')})")
    ratio = holds / tot if tot else 0.0
    regime = "risk_on" if ratio >= 0.66 else ("neutral" if ratio >= 0.34 else "risk_off")
    return {"regime": regime, "indexAboveMA20": f"{holds}/{tot}",
            "deployFrac": DEPLOY[regime], "detail": detail}

# Liquid US momentum universe (fractional-friendly on Toss AMOUNT orders).
UNIVERSE_US = [
    "AAPL", "MSFT", "NVDA", "GOOGL", "META", "AMZN", "AVGO", "AMD",
    "NFLX", "TSLA", "LLY", "COST", "CRM", "ORCL",
]


MAX_ATR_PCT = 6.0   # #24: skip names too volatile to risk-manage with a -7% stop

# 급등주 책 규칙(book-compliant 모드): 거래량 폭발 + 전고점 돌파만 진입.
# "거래량 없는 상승은 위험"(책 #1). 켜면 진입이 극도로 selective해진다(=생존).
BOOK_MODE = True
BOOK_VOL_SPIKE = 2.0   # 당일 거래량배수(volRatio) ≥ 2.0× 평균


def _ret20(candles):
    try:
        closes = [float(c["close"]) for c in candles]
        return round((closes[-1] / closes[-21] - 1) * 100, 2)
    except Exception:
        return None


def score_symbol(sym, candles, idx_ret20):
    """Score ONE symbol's candle series → (candidate dict | None, reject reason).
    SINGLE SOURCE OF TRUTH shared by live scan() AND backtest.py so both test the
    IDENTICAL entry rules. `candles` is oldest-first; for backtest pass the slice
    up to (and including) the decision bar — no look-ahead. `idx_ret20` = the
    benchmark's 20-day return at that same point (None to skip the RS gate)."""
    import indicators as IND
    if not candles or len(candles) < 60:
        return None, "캔들 부족"
    indd = IND.compute(candles)
    if indd.get("last") is None:
        return None, "지표 없음"
    e = TR.evaluate_entry(indd)
    if e["signal"] != "BUY_CANDIDATE":
        return None, e["reason"]
    atr = TR.atr_pct(candles, 14)                       # #24 volatility gate
    if atr is not None and atr > MAX_ATR_PCT:
        return None, f"ATR {atr}% > {MAX_ATR_PCT}% 과변동"
    sr = TR.support_resistance(candles[:-1], 40)        # resistance from PRIOR bars
    bo = TR.breakout_confirmed(indd["last"], sr.get("resistance"), indd.get("volRatio") or 0)
    if BOOK_MODE:                                       # 급등주 책 하드게이트
        if not bo["breakout"]:
            return None, "책모드: 전고점 돌파 아님(거래량 동반 돌파만)"
        vr = indd.get("volRatio")
        if vr is None or vr < BOOK_VOL_SPIKE:
            return None, f"책모드: 거래량 {vr}배 < {BOOK_VOL_SPIKE}배(거래량 폭발 필요)"
    r20 = _ret20(candles)
    rs20 = round(r20 - idx_ret20, 2) if (r20 is not None and idx_ret20 is not None) else None
    if rs20 is not None and rs20 < 0:
        return None, f"RS {rs20:+.1f} — 지수 대비 약세"
    gap = None
    try:
        gap = round((float(candles[-1]["open"]) / float(candles[-2]["close"]) - 1) * 100, 2)
    except Exception:
        pass
    last, ma20 = indd["last"], indd.get("ma20")
    ext = (last / ma20 - 1) * 100 if ma20 else 0.0
    score = ((indd.get("scalpScore") or 0)
             + (30 if bo["breakout"] else 0)
             + min(rs20 if rs20 is not None else 0, 15)
             - abs(ext - 4) * 2
             - (10 if (atr or 0) > 4.5 else 0)
             - (8 if gap is not None and abs(gap) >= 4 else 0))
    return {
        "symbol": sym, "last": last, "rsi": indd.get("rsi14"), "ma20": ma20,
        "extPct": round(ext, 1), "scalp": indd.get("scalpScore"), "atrPct": atr,
        "breakout": bo["breakout"], "ret20": r20, "rs20": rs20,
        "gapPct": gap if (gap and abs(gap) >= 4) else None,
        "tier": "A돌파" if bo["breakout"] else "B추세",
        "support": sr.get("support"), "resistance": sr.get("resistance"),
        "score": round(score, 1), "reason": bo["reason"],
    }, None


def scan(client, analyze=None) -> Dict:
    """Rank BUY candidates via score_symbol (shared with backtest.py). One candles
    fetch per symbol. `analyze` accepted for backward-compat and ignored."""
    idx_ret20 = None
    for idx_sym in ("SPY", "360750"):          # #1 RS benchmark
        try:
            ic = client.get_candles(idx_sym, "1d", 30).get("candles", [])
            idx_ret20 = _ret20(ic)
            if idx_ret20 is not None:
                break
        except Exception:
            continue
    cands: List[Dict] = []
    rejected: List[Dict] = []
    for sym in UNIVERSE_US:
        candles = None
        for _ in range(2):
            try:
                candles = client.get_candles(sym, "1d", 160).get("candles", [])
                if candles:
                    break
            except Exception:  # noqa: BLE001
                candles = None
        cand, reason = score_symbol(sym, candles or [], idx_ret20)
        if cand:
            cands.append(cand)
        else:
            rejected.append({"symbol": sym, "reason": reason})
    cands.sort(key=lambda c: (not c["breakout"], -c["score"]))
    return {"candidates": cands, "rejected": rejected}


def plan_for(top: Dict, usdkrw: float) -> Dict:
    """Build the (dry-run) trade parameters for the top candidate."""
    r = TR.RULESETS[RULESET]
    usd_amount = round(CAPITAL_KRW / usdkrw, 2)
    entry = top["last"]
    stop = round(entry * (1 + r["stop_pct"] / 100.0), 2)
    target = round(entry * (1 + r["take_pct"] / 100.0), 2)
    return {
        "symbol": top["symbol"], "side": "BUY", "orderType": "MARKET",
        "orderAmountUSD": usd_amount, "approxKRW": CAPITAL_KRW,
        "entryRef": entry, "stopLoss": stop, "takeProfit": target,
        "maxLossKRW": int(CAPITAL_KRW * abs(r["stop_pct"]) / 100.0),
        "ruleset": r["label"],
    }
