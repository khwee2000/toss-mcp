"""backtest.py — walk-forward backtest of the DEPLOYED trading rules.

Answers the only question that matters before real money: does this strategy
have an edge — after costs — versus just holding QQQ?

Design (honest, no look-ahead):
  • Reuses the LIVE decision functions verbatim — strategy_100k.score_symbol
    (entry + ranking) and market_cycle.evaluate_position (stop/trail/trend/TP/
    time exits). No re-implementation, so the backtest tests what actually ships.
  • Event-driven PORTFOLIO sim over the union of US trading days. Decisions are
    made on the CLOSE of day t using only data ≤ t; fills happen at the OPEN of
    day t+1 (no look-ahead / no same-bar fill).
  • Costs on every leg: commission + half-spread + FX (round trip ≈ 0.7%).
  • Regime from SPY vs its MA20 (a US proxy, per the audit). Deploy fraction and
    a max of MAX_POSITIONS concurrent names, each sized like live.
  • Split by date into In-Sample (older 60%) and Out-Of-Sample (recent 40%).

Verdict gate (audit): OOS ProfitFactor > 1.3 AND OOS portfolio return beats
cost-adjusted QQQ buy-&-hold over the SAME window. Otherwise: NO edge → do not
trade real money; keep paper.

Usage: python3.12 backtest.py [pages]     (pages of ~190 bars each, default 4)
"""
from __future__ import annotations
import sys
from datetime import date, datetime

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S            # noqa: E402
import indicators as IND      # noqa: E402
import trading_rules as TR    # noqa: E402
import strategy_100k as ST    # noqa: E402
import market_cycle as MC     # noqa: E402

WARMUP = 130                  # bars before the first decision (MA120 etc.)
LEG_COST = (0.1 + 0.15 + 0.1) / 100.0   # commission + half-spread + FX, per side
MAX_POSITIONS = 2
DEPLOY = {"risk_on": 0.90, "risk_off": 0.60}
BENCH = "QQQ"
RS_BENCH = "SPY"
COOLDOWN_D = 2


def fetch_hist(sym, pages=4, per=190):
    """Paginate get_candles (nextBefore) → ascending, de-duplicated daily bars."""
    acc, before = {}, None
    for _ in range(pages):
        r = None
        for _try in range(3):
            try:
                r = (S.CLIENT.get_candles(sym, "1d", per, before=before)
                     if before else S.CLIENT.get_candles(sym, "1d", per))
                break
            except Exception:
                import time
                time.sleep(1.5)
        if not r:
            break
        cs = r.get("candles", [])
        if not cs:
            break
        for c in cs:
            acc[str(c["timestamp"])[:10]] = c
        before = r.get("nextBefore")
        if not before:
            break
    return [acc[k] for k in sorted(acc)]


def _d(ts):
    return date.fromisoformat(str(ts)[:10])


def _ret20_series(closes):
    """trailing 20-bar % return at each index (None for first 20)."""
    out = []
    for i in range(len(closes)):
        out.append(round((closes[i] / closes[i - 20] - 1) * 100, 2) if i >= 20 else None)
    return out


def load_all(pages):
    print(f"히스토리 로딩(≈{pages}페이지)…")
    data = {}
    for sym in ST.UNIVERSE_US + [BENCH, RS_BENCH]:
        rows = fetch_hist(sym, pages)
        if len(rows) >= WARMUP + 20:
            data[sym] = {"rows": rows,
                         "byDate": {str(r["timestamp"])[:10]: i for i, r in enumerate(rows)},
                         "close": [float(r["close"]) for r in rows]}
        else:
            print(f"  ⚠️ {sym}: {len(rows)}봉 — 부족, 제외")
    return data


def run(pages=4):
    data = load_all(pages)
    if BENCH not in data or RS_BENCH not in data:
        print("벤치마크/RS 데이터 부족 — 중단"); return
    universe = [s for s in ST.UNIVERSE_US if s in data]
    bench = data[BENCH]
    rs = data[RS_BENCH]
    rs_r20 = {str(r["timestamp"])[:10]: v
              for r, v in zip(rs["rows"], _ret20_series(rs["close"]))}
    # SPY MA20 regime by date
    spy_close = rs["close"]
    spy_ma20 = {str(rs["rows"][i]["timestamp"])[:10]:
                (sum(spy_close[i - 19:i + 1]) / 20 if i >= 19 else None)
                for i in range(len(spy_close))}

    # master timeline = benchmark trading days (US calendar)
    dates = [str(r["timestamp"])[:10] for r in bench["rows"]]
    start_i = WARMUP
    split_i = start_i + int((len(dates) - start_i) * 0.60)   # IS / OOS boundary
    split_date = dates[split_i]
    print(f"기간: {dates[start_i]} ~ {dates[-1]} ({len(dates)-start_i}일) | "
          f"IS<{split_date}≤OOS | 종목 {len(universe)}\n")

    cash = 1.0
    positions = {}     # sym -> {entry_px, entry_date, highwater, tp1_done, size}
    cooldown = {}      # sym -> date until
    equity_curve = []  # (date, equity)
    trades = []        # closed legs: {sym, entry_d, exit_d, netRet, days, signal, seg}

    def mtm(di):
        d = dates[di]
        val = cash
        for sym, p in positions.items():
            c = data[sym]["byDate"].get(d)
            px = data[sym]["close"][c] if c is not None else p["entry_px"]
            val += p["size"] * (px / p["entry_px"])
        return val

    for di in range(start_i, len(dates) - 1):
        d = dates[di]
        nd = dates[di + 1]
        # ---------- EXITS (decide on close d, fill open nd) ----------
        for sym in list(positions.keys()):
            ds = data[sym]
            ci = ds["byDate"].get(d)
            if ci is None or ci < 60:
                continue
            sl = ds["rows"][:ci + 1]
            ind = IND.compute(sl)
            ind["atrPct"] = TR.atr_pct(sl, 14)
            p = positions[sym]
            pdict = {"symbol": sym, "entryRef": p["entry_px"], "highwater": p["highwater"],
                     "entryDate": p["entry_date"], "tp1_done": p["tp1_done"]}
            try:
                e = MC.evaluate_position(pdict, cur=ds["close"][ci], s=ind, today=_d(d))
            except Exception:
                continue
            p["highwater"] = e.get("highwater", p["highwater"])
            sig = e["signal"]
            if sig in ("HOLD", "DATA?"):
                continue
            ni = data[sym]["byDate"].get(nd)
            fill = float(ds["rows"][ni]["open"]) if ni is not None else ds["close"][ci]
            portion = p["size"] * (0.5 if sig == "TP1_HALF" else 1.0)
            gross = fill / p["entry_px"] - 1
            net = gross - 2 * LEG_COST          # round trip for this portion
            cash += portion * (1 + net)
            seg = "IS" if d < split_date else "OOS"
            trades.append({"sym": sym, "entry_d": p["entry_date"], "exit_d": d,
                           "netRet": net, "days": (_d(d) - _d(p["entry_date"])).days,
                           "signal": sig, "seg": seg})
            if sig == "TP1_HALF":
                p["size"] -= portion
                if fill >= p["entry_px"]:      # only mark done if TP1 truly hit in profit
                    p["tp1_done"] = True
            else:
                if sig in ("STOP", "TRAIL_STOP") or gross < 0:
                    cooldown[sym] = (_d(d).toordinal() + COOLDOWN_D)
                del positions[sym]
        # ---------- ENTRIES (rank on close d, fill open nd) ----------
        if len(positions) < MAX_POSITIONS:
            ma = spy_ma20.get(d)
            spy_c = rs["close"][rs["byDate"][d]] if d in rs["byDate"] else None
            regime = "risk_on" if (ma and spy_c and spy_c > ma) else "risk_off"
            dep = DEPLOY[regime]
            idxr = rs_r20.get(d)
            cands = []
            for sym in universe:
                if sym in positions:
                    continue
                if sym in cooldown and _d(d).toordinal() < cooldown[sym]:
                    continue
                ci = data[sym]["byDate"].get(d)
                if ci is None or ci < 60:
                    continue
                cand, _r = ST.score_symbol(sym, data[sym]["rows"][:ci + 1], idxr)
                if cand:
                    # risk_off: breakout(A)만 (live 규칙 반영)
                    if regime == "risk_off" and not cand["breakout"]:
                        continue
                    cands.append(cand)
            cands.sort(key=lambda c: (not c["breakout"], -c["score"]))
            slot = min(dep / MAX_POSITIONS, 0.60)
            for cand in cands:
                if len(positions) >= MAX_POSITIONS or cash < slot:
                    break
                sym = cand["symbol"]
                ni = data[sym]["byDate"].get(nd)
                if ni is None:
                    continue
                fill = float(data[sym]["rows"][ni]["open"]) * (1 + LEG_COST)  # buy cost in entry px
                cash -= slot
                positions[sym] = {"entry_px": fill, "entry_date": nd,
                                  "highwater": fill, "tp1_done": False, "size": slot}
        equity_curve.append((d, mtm(di)))

    # liquidate at final close
    last_d = dates[-1]
    for sym, p in positions.items():
        ci = data[sym]["byDate"].get(last_d)
        px = data[sym]["close"][ci] if ci is not None else p["entry_px"]
        net = (px / p["entry_px"] - 1) - 2 * LEG_COST
        cash += p["size"] * (1 + net)
        trades.append({"sym": sym, "entry_d": p["entry_date"], "exit_d": last_d,
                       "netRet": net, "days": (_d(last_d) - _d(p["entry_date"])).days,
                       "signal": "EOD", "seg": "IS" if p["entry_date"] < split_date else "OOS"})
    equity_curve.append((last_d, cash))
    _report(trades, equity_curve, dates, start_i, split_i, bench)


def _stats(trades):
    if not trades:
        return {"n": 0}
    wins = [t["netRet"] for t in trades if t["netRet"] > 0]
    losses = [t["netRet"] for t in trades if t["netRet"] <= 0]
    tot = sum(t["netRet"] for t in trades)
    pf = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else float("inf")
    return {"n": len(trades), "win": len(wins), "loss": len(losses),
            "winRate": round(len(wins) / len(trades) * 100, 1),
            "avgWin": round(sum(wins) / len(wins) * 100, 2) if wins else 0.0,
            "avgLoss": round(sum(losses) / len(losses) * 100, 2) if losses else 0.0,
            "expectancy": round(tot / len(trades) * 100, 3),
            "pf": round(pf, 2) if pf != float("inf") else 99.9,
            "avgHold": round(sum(t["days"] for t in trades) / len(trades), 1)}


def _mdd(curve):
    peak, mdd = -1e9, 0.0
    for _, v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    return round(mdd * 100, 1)


def _bh(bench, i0, i1):
    """QQQ buy&hold total return between benchmark indices, net of one round trip."""
    c0, c1 = bench["close"][i0], bench["close"][i1]
    return round((c1 / c0 - 1 - 2 * LEG_COST) * 100, 1)


def _report(trades, curve, dates, start_i, split_i, bench):
    def show(seg):
        st = _stats([t for t in trades if t["seg"] == seg] if seg else trades)
        tag = seg or "전체"
        if not st.get("n"):
            print(f"[{tag}] 거래 없음"); return None
        print(f"[{tag}] 거래 {st['n']} (승 {st['win']}/패 {st['loss']}) | 승률 {st['winRate']}% | "
              f"PF {st['pf']} | 기대값 {st['expectancy']:+}%/트레이드 | "
              f"평균승 {st['avgWin']}% 평균패 {st['avgLoss']}% | 보유 {st['avgHold']}일")
        return st
    print("=" * 72)
    print("백테스트 결과 (실전 규칙 재현, 비용차감, look-ahead 없음)")
    print("=" * 72)
    # 최근 1개월(21거래일) 구간 — '지금 세팅으로 최근 한달' 성과
    if len(curve) >= 22:
        m0, m1 = curve[-22][1], curve[-1][1]
        mdd_m = _mdd(curve[-22:])
        d_from = curve[-22][0]
        mtr = [t for t in trades if t["exit_d"] >= d_from]
        print(f"[최근 1개월 ≈21거래일 · {d_from}~{curve[-1][0]}]  수익 {(m1/m0-1)*100:+.2f}%  "
              f"| 낙폭 {mdd_m}%  | 거래 {len(mtr)}건")
        # 같은 창의 QQQ
        try:
            bi = bench["byDate"]; qi0 = bi.get(d_from); qi1 = len(bench["close"]) - 1
            if qi0 is not None:
                print(f"    (같은 창 QQQ 매수보유 {_bh(bench, qi0, qi1):+.2f}%)")
        except Exception:
            pass
        print("-" * 72)
    show(None); is_s = show("IS"); oos = show("OOS")
    fin = curve[-1][1]
    print(f"\n포트폴리오 최종 자산: {fin:.3f} (시작 1.000) → 총수익 {(fin-1)*100:+.1f}% | 최대낙폭 {_mdd(curve)}%")
    # benchmark over full + OOS windows
    bi = bench["byDate"]
    d0, dsplit, dlast = dates[start_i], dates[split_i], dates[-1]
    i0 = bi.get(d0, start_i); isp = bi.get(dsplit, split_i); il = bi.get(dlast, len(bench["close"]) - 1)
    print(f"벤치마크 QQQ 매수보유: 전체 {_bh(bench, i0, il):+.1f}% | OOS {_bh(bench, isp, il):+.1f}% (비용차감)")
    # verdict
    print("\n" + "-" * 72)
    oos_pf = oos["pf"] if oos else 0
    oos_ret = None
    oos_curve = [(d, v) for d, v in curve if d >= dsplit]
    if len(oos_curve) >= 2:
        oos_ret = round((oos_curve[-1][1] / oos_curve[0][1] - 1) * 100, 1)
    qqq_oos = _bh(bench, isp, il)
    beats = (oos_ret is not None and oos_ret > qqq_oos)
    gate = (oos_pf > 1.3) and beats
    print(f"판정 게이트: OOS PF>1.3 AND OOS수익>QQQ")
    print(f"  OOS PF = {oos_pf} ( >1.3 ? {'YES' if oos_pf>1.3 else 'NO'} )")
    print(f"  OOS 전략 {oos_ret}% vs QQQ {qqq_oos}%  ( 초과 ? {'YES' if beats else 'NO'} )")
    print(f"\n  >>> 판정: {'✅ 엣지 있음 — 실전 후보(단 리스크 보완 후)' if gate else '❌ 엣지 미검증 — 실전 금지, 페이퍼 유지'}")
    print("-" * 72)
    print("주의: 가정(반쪽스프레드 0.15%·비용 0.7%왕복·SPY-MA20 레짐·소표본)으로 근사치.")


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 4)
