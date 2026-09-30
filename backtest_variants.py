"""backtest_variants.py — test EVIDENCE-BASED redesigns vs QQQ, honestly.

The original 'daily breakout scalp' already failed OOS. This tests three redesigns
that have actual literature behind them, so we deploy only what passes:

  V1  INDEX TREND-FOLLOW : hold QQQ while its close > 200-day MA, else cash.
                           (classic time-series momentum / trend filter — cuts
                            drawdown, few trades/yr → tiny cost.)
  V2  DUAL MOMENTUM      : monthly, only if SPY>200MA, hold the top-2 universe
                           names by 3-month return (cross-sectional momentum,
                           the one factor with robust evidence), else cash.
  V3  BUY & HOLD QQQ     : the benchmark to beat.

No look-ahead (decide on close t, fill open t+1). Costs on every leg. Reports
full + out-of-sample return, max drawdown, and #trades. Reuses backtest.py data.

Usage: python3.12 backtest_variants.py [pages]   (default 5 ≈ 3+ yrs)
"""
from __future__ import annotations
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as BT     # noqa: E402  (fetch/load/_mdd/_d/LEG_COST)

WARMUP = 205              # need 200-day MA
REBAL = 21               # ~monthly (trading days)
TOPN = 2
MOM_LOOKBACK = 63        # ~3-month return for cross-sectional momentum


def _ma(closes, i, n):
    return sum(closes[i - n + 1:i + 1]) / n if i >= n - 1 else None


def variant_index_trend(data, dates, start_i):
    """Hold BENCH when close>200MA (decided close t, filled open t+1), else cash."""
    b = data[BT.BENCH]
    curve, in_mkt, trades = [], False, 0
    equity, ref_px = 1.0, None
    for di in range(start_i, len(dates) - 1):
        d, nd = dates[di], dates[di + 1]
        i = b["byDate"].get(d)
        if i is None:
            continue
        # mark to market
        if in_mkt and ref_px:
            equity = held_cash * (b["close"][i] / ref_px)
        sig = b["close"][i] > (_ma(b["close"], i, 200) or 1e9)
        ni = b["byDate"].get(nd)
        fill = float(b["rows"][ni]["open"]) if ni is not None else b["close"][i]
        if sig and not in_mkt:
            held_cash = equity * (1 - BT.LEG_COST)
            ref_px, in_mkt, trades = fill, True, trades + 1
            equity = held_cash
        elif not sig and in_mkt:
            equity = held_cash * (fill / ref_px) * (1 - BT.LEG_COST)
            in_mkt, ref_px = False, None
            held_cash = equity
        curve.append((d, equity))
    return curve, trades


def variant_momentum(data, universe, dates, start_i):
    """Monthly: if SPY>200MA hold top-2 by 3mo return (equal wt), else cash."""
    spy = data[BT.RS_BENCH]
    curve, trades = [], 0
    equity = 1.0
    holds = {}      # sym -> (ref_px, cash_alloc)
    for di in range(start_i, len(dates) - 1):
        d, nd = dates[di], dates[di + 1]
        si = spy["byDate"].get(d)
        if si is None:
            continue
        # mark to market
        val = equity - sum(c for _, c in holds.values())  # leftover cash
        for sym, (rp, c) in holds.items():
            ci = data[sym]["byDate"].get(d)
            px = data[sym]["close"][ci] if ci is not None else rp
            val += c * (px / rp)
        equity = val
        if (di - start_i) % REBAL == 0:                    # rebalance day
            risk_on = spy["close"][si] > (_ma(spy["close"], si, 200) or 1e9)
            ranked = []
            if risk_on:
                for sym in universe:
                    ci = data[sym]["byDate"].get(d)
                    if ci is None or ci < MOM_LOOKBACK:
                        continue
                    mom = data[sym]["close"][ci] / data[sym]["close"][ci - MOM_LOOKBACK] - 1
                    ranked.append((mom, sym))
                ranked.sort(reverse=True)
            target = [s for _, s in ranked[:TOPN]]
            # liquidate all at next open (simple full-rebalance), then buy targets
            new_holds = {}
            per = (equity / len(target)) if target else 0.0
            for sym in set(list(holds) + target):
                ni = data[sym]["byDate"].get(nd) if sym in target or sym in holds else None
                if sym in holds and sym not in target:      # sell
                    trades += 1
                if sym in target:
                    ci = data[sym]["byDate"].get(nd)
                    if ci is None:
                        continue
                    fill = float(data[sym]["rows"][ci]["open"])
                    if sym not in holds:
                        trades += 1
                    new_holds[sym] = (fill, per * (1 - BT.LEG_COST))
            # apply cost for churn already folded via (1-LEG_COST) on new allocations
            holds = new_holds
        curve.append((d, equity))
    return curve, trades


def _seg_ret(curve, dsplit):
    full = round((curve[-1][1] / curve[0][1] - 1) * 100, 1) if len(curve) >= 2 else 0.0
    oos = [(d, v) for d, v in curve if d >= dsplit]
    oret = round((oos[-1][1] / oos[0][1] - 1) * 100, 1) if len(oos) >= 2 else None
    return full, oret, BT._mdd(curve), BT._mdd(oos) if len(oos) >= 2 else 0.0


def run(pages=5):
    data = BT.load_all(pages)
    if BT.BENCH not in data or BT.RS_BENCH not in data:
        print("벤치 데이터 부족"); return
    universe = [s for s in __import__("strategy_100k").UNIVERSE_US if s in data]
    b = data[BT.BENCH]
    dates = [str(r["timestamp"])[:10] for r in b["rows"]]
    start_i = WARMUP
    split_i = start_i + int((len(dates) - start_i) * 0.60)
    dsplit = dates[split_i]
    print(f"기간 {dates[start_i]}~{dates[-1]} ({len(dates)-start_i}일) | OOS≥{dsplit} | 종목 {len(universe)}\n")

    # V3 buy&hold QQQ
    i0, il = b["byDate"].get(dates[start_i], start_i), len(b["close"]) - 1
    bh_curve = [(dates[di], b["close"][b["byDate"][dates[di]]] / b["close"][i0] * (1 - BT.LEG_COST))
                for di in range(start_i, len(dates)) if dates[di] in b["byDate"]]
    v1c, v1t = variant_index_trend(data, dates, start_i)
    v2c, v2t = variant_momentum(data, universe, dates, start_i)

    print("=" * 74)
    print(f"{'전략':<26}{'전체수익':>10}{'OOS수익':>10}{'전체MDD':>10}{'OOS MDD':>10}{'거래':>7}")
    print("-" * 74)
    for name, curve, tr in [("V3 QQQ 매수보유(벤치)", bh_curve, 1),
                            ("V1 인덱스 추세추종(200MA)", v1c, v1t),
                            ("V2 듀얼모멘텀(월,top2)", v2c, v2t)]:
        f, o, mdd, omdd = _seg_ret(curve, dsplit)
        print(f"{name:<24}{f:>9}%{('' if o is None else o):>9}%{mdd:>9}%{omdd:>9}%{tr:>7}")
    print("=" * 74)
    qf, qo, qmdd, qomdd = _seg_ret(bh_curve, dsplit)
    print("\n판정(OOS 기준, 벤치=QQQ):")
    for name, curve in [("V1 인덱스 추세추종", v1c), ("V2 듀얼모멘텀", v2c)]:
        _, o, _, omdd = _seg_ret(curve, dsplit)
        beats_ret = (o is not None and o > qo)
        lower_dd = omdd > qomdd            # less negative = shallower drawdown
        verdict = ("✅ 유의미(수익↑ 또는 낙폭↓ 위험조정 우위)" if (beats_ret or lower_dd)
                   else "❌ 벤치 대비 우위 없음")
        print(f"  {name}: OOS {o}% vs QQQ {qo}% | OOS낙폭 {omdd}% vs {qomdd}% → {verdict}")
    print("\n주의: 소표본·비용가정(0.35%/leg)·2024-26 강세장 편향. 과최적화 경계.")


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
