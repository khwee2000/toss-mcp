"""backtest_book.py — 두 급등주 책의 규칙을 하드 게이트로 인코딩해 백테스트.

책의 핵심 진입 규칙(둘 다 수렴):
  · 거래량 폭발      : 당일 거래량 ≥ VOL_SPIKE × 20일 평균 (책: 평소 대비 몇 배)
  · 전고점 돌파/신고가 : 종가 > 직전 N봉 최고가 (대장주 = 신고가 경신)
  · 불장 레짐        : 시장(SPY)이 MA50 위 — "불장에서만, 침체/붕괴엔 현금"
  · 비과열(끝물 회피) : MA20 대비 +EXT_MAX% 이내 (마지막 폭등 추격 금지)
  · 강한 놈 우선     : 후보를 거래대금(turnover)·RS로 랭킹 → 대장주 집중
청산(책: 짧게):
  · 익절  +TP1% 절반, +TP2% 잔량   (3~10%, 욕심 금지)
  · 손절  -STOP% (칼손절)
  · 추세이탈 MA20 종가 이탈 → 정리
현금관리: 동시 MAX_POS 종목, regime risk_off면 진입 안 함(쉬는 것도 매매).

정직한 한계: 책은 본질적으로 '분(minute)단위 시간대 + 소형 급등주(25~200% 무버)'
전략이다. 이 백테스트는 '일봉 + 제한된 유니버스'라 시간대·소형주 알파는 검증 불가.
여기서 검증하는 것은 '책의 일봉 하드필터가 통계적 엣지를 만드는가'뿐이다.

Usage: python3.12 backtest_book.py [pages]  (default 5)
"""
from __future__ import annotations
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as BT     # noqa: E402  (load_all, LEG_COST, _mdd, _bh, _d, BENCH, RS_BENCH)
import indicators as IND  # noqa: E402
import trading_rules as TR  # noqa: E402
import strategy_100k as ST  # noqa: E402

# ---- 책 규칙 파라미터 ----
VOL_SPIKE = 3.0        # 당일 거래량 ≥ 3× 20일평균 (일봉 프록시; 책 원문은 10~50×)
EXT_MAX = 15.0         # MA20 대비 +15% 초과 = 끝물, 추격 금지
BREAKOUT_LB = 40       # 전고점 룩백(봉)
TP1, TP2 = 5.0, 10.0   # 짧은 익절
STOP = -8.0            # 칼손절(미국 변동성 반영 -8%)
TRAIL = 10.0
MAX_POS = 2
WARMUP = 205


def _ma(c, i, n):
    return sum(c[i - n + 1:i + 1]) / n if i >= n - 1 else None


def run(pages=5):
    data = BT.load_all(pages)
    if BT.BENCH not in data or BT.RS_BENCH not in data:
        print("벤치 데이터 부족"); return
    uni = [s for s in ST.UNIVERSE_US if s in data]
    b, spy = data[BT.BENCH], data[BT.RS_BENCH]
    dates = [str(r["timestamp"])[:10] for r in b["rows"]]
    start_i = WARMUP
    split_i = start_i + int((len(dates) - start_i) * 0.60)
    dsplit = dates[split_i]
    spy_ma50 = {str(spy["rows"][i]["timestamp"])[:10]: _ma(spy["close"], i, 50)
                for i in range(len(spy["close"]))}
    print(f"기간 {dates[start_i]}~{dates[-1]} ({len(dates)-start_i}일) | OOS≥{dsplit} | 종목 {len(uni)}")
    print(f"책 게이트: 거래량≥{VOL_SPIKE}×평균 · 전고점돌파 · SPY>MA50(불장) · MA20+{EXT_MAX}%이내\n")

    cash, positions, trades, curve = 1.0, {}, [], []

    def book_entry(sym, ci):
        """책 하드게이트 통과 여부 → 후보 dict or None."""
        rows = data[sym]["rows"]
        if ci < 60:
            return None
        sl = rows[:ci + 1]
        ind = IND.compute(sl)
        last, ma20 = ind.get("last"), ind.get("ma20")
        if not last or not ma20:
            return None
        # 정배열 + RSI 밴드 (기본 추세)
        if ind.get("arrange") != "정배열" or last <= ma20:
            return None
        rsi = ind.get("rsi14")
        if rsi is not None and not (45 <= rsi <= 72):
            return None
        # 거래량 폭발
        vols = [float(r.get("volume") or 0) for r in rows[ci - 20:ci]]
        avgv = sum(vols) / len(vols) if vols else 0
        vtoday = float(rows[ci].get("volume") or 0)
        if avgv <= 0 or vtoday < VOL_SPIKE * avgv:
            return None
        # 전고점 돌파(신고가) — 직전 봉들 최고가 초과
        prior_high = max(float(r["high"]) for r in rows[ci - BREAKOUT_LB:ci])
        if last <= prior_high:
            return None
        # 끝물 회피
        if (last / ma20 - 1) * 100 > EXT_MAX:
            return None
        turnover = last * vtoday       # 거래대금 프록시 → 강한 놈 랭킹
        return {"symbol": sym, "score": turnover, "entry_ci": ci}

    for di in range(start_i, len(dates) - 1):
        d, nd = dates[di], dates[di + 1]
        # EXITS
        for sym in list(positions):
            ds = data[sym]; ci = ds["byDate"].get(d)
            if ci is None:
                continue
            p = positions[sym]
            cur = ds["close"][ci]
            p["hw"] = max(p["hw"], cur)
            ind = IND.compute(ds["rows"][:ci + 1]); ma20 = ind.get("ma20")
            plp = (cur / p["entry"] - 1) * 100
            trail_stop = p["hw"] * (1 - TRAIL / 100)
            sig = None
            if plp <= STOP or cur <= trail_stop:
                sig = "STOP"
            elif ma20 and cur < ma20:
                sig = "TREND"
            elif not p["tp1"] and plp >= TP1:
                sig = "TP1"
            elif plp >= TP2:
                sig = "TP2"
            if sig:
                ni = ds["byDate"].get(nd)
                fill = float(ds["rows"][ni]["open"]) if ni is not None else cur
                portion = p["size"] * (0.5 if sig == "TP1" else 1.0)
                net = (fill / p["entry"] - 1) - 2 * BT.LEG_COST
                cash += portion * (1 + net)
                trades.append({"sym": sym, "net": net, "sig": sig,
                               "days": (BT._d(d) - BT._d(p["ed"])).days,
                               "seg": "IS" if d < dsplit else "OOS"})
                if sig == "TP1":
                    p["size"] -= portion; p["tp1"] = True
                else:
                    del positions[sym]
        # ENTRIES — 불장 레짐 + 책 게이트, 대장주(거래대금) 우선
        risk_on = (spy["close"][spy["byDate"][d]] > spy_ma50.get(d, 1e9)) if d in spy["byDate"] else False
        if risk_on and len(positions) < MAX_POS:
            cands = []
            for sym in uni:
                if sym in positions:
                    continue
                ci = data[sym]["byDate"].get(d)
                if ci is None:
                    continue
                c = book_entry(sym, ci)
                if c:
                    cands.append(c)
            cands.sort(key=lambda c: -c["score"])   # 거래대금 최상위 = 대장주
            slot = min(0.90 / MAX_POS, 0.45)
            for c in cands:
                if len(positions) >= MAX_POS or cash < slot:
                    break
                sym = c["symbol"]; ni = data[sym]["byDate"].get(nd)
                if ni is None:
                    continue
                fill = float(data[sym]["rows"][ni]["open"]) * (1 + BT.LEG_COST)
                cash -= slot
                positions[sym] = {"entry": fill, "ed": nd, "hw": fill, "tp1": False, "size": slot}
        # MTM
        val = cash
        for sym, p in positions.items():
            ci = data[sym]["byDate"].get(d)
            px = data[sym]["close"][ci] if ci is not None else p["entry"]
            val += p["size"] * (px / p["entry"])
        curve.append((d, val))

    # liquidate
    ld = dates[-1]
    for sym, p in positions.items():
        ci = data[sym]["byDate"].get(ld)
        px = data[sym]["close"][ci] if ci is not None else p["entry"]
        net = (px / p["entry"] - 1) - 2 * BT.LEG_COST
        cash += p["size"] * (1 + net)
        trades.append({"sym": sym, "net": net, "sig": "EOD",
                       "days": (BT._d(ld) - BT._d(p["ed"])).days,
                       "seg": "IS" if p["ed"] < dsplit else "OOS"})
    curve.append((ld, cash))
    _report(trades, curve, dates, start_i, split_i, b, dsplit)


def _stat(ts):
    if not ts:
        return None
    w = [t["net"] for t in ts if t["net"] > 0]; l = [t["net"] for t in ts if t["net"] <= 0]
    tot = sum(t["net"] for t in ts)
    pf = sum(w) / abs(sum(l)) if l and sum(l) != 0 else 99.9
    return {"n": len(ts), "wr": round(len(w) / len(ts) * 100, 1),
            "pf": round(pf, 2), "exp": round(tot / len(ts) * 100, 2),
            "aw": round(sum(w) / len(w) * 100, 2) if w else 0,
            "al": round(sum(l) / len(l) * 100, 2) if l else 0}


def _report(trades, curve, dates, si, spi, b, dsplit):
    print("=" * 70)
    print("책 규칙 백테스트 (하드게이트, 비용차감, look-ahead 없음)")
    print("=" * 70)
    for seg in (None, "IS", "OOS"):
        s = _stat(trades if seg is None else [t for t in trades if t["seg"] == seg])
        tag = seg or "전체"
        if not s:
            print(f"[{tag}] 거래 없음"); continue
        print(f"[{tag}] 거래 {s['n']} | 승률 {s['wr']}% | PF {s['pf']} | "
              f"기대값 {s['exp']:+}% | 평균승 {s['aw']}% 평균패 {s['al']}%")
    fin = curve[-1][1]
    print(f"\n포트폴리오 총수익 {(fin-1)*100:+.1f}% | 최대낙폭 {BT._mdd(curve)}%")
    bi = b["byDate"]; i0 = bi.get(dates[si], si); isp = bi.get(dates[spi], spi); il = len(b["close"]) - 1
    print(f"벤치 QQQ 매수보유: 전체 {BT._bh(b, i0, il):+.1f}% | OOS {BT._bh(b, isp, il):+.1f}%")
    oos = [(d, v) for d, v in curve if d >= dsplit]
    oret = round((oos[-1][1] / oos[0][1] - 1) * 100, 1) if len(oos) >= 2 else None
    qoos = BT._bh(b, isp, il)
    oos_s = _stat([t for t in trades if t["seg"] == "OOS"])
    opf = oos_s["pf"] if oos_s else 0
    gate = (opf > 1.3) and (oret is not None and oret > qoos)
    print("\n" + "-" * 70)
    print(f"판정: OOS PF {opf} (>1.3? {'Y' if opf>1.3 else 'N'}) | OOS전략 {oret}% vs QQQ {qoos}% "
          f"({'초과' if (oret is not None and oret>qoos) else '미달'})")
    print(f"  >>> {'✅ 엣지 있음' if gate else '❌ 엣지 미검증 — 페이퍼 유지'}")
    print("-" * 70)
    print("한계: 일봉+제한 유니버스 → 책의 '분단위 시간대·소형 급등주' 알파는 검증 불가.")


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 5)
