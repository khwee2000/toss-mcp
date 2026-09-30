"""backtest_anticipation.py — 사용자 통찰 검증: "기대감에 오르면 판다".

핵심 아이디어(=역추세/평균회귀): 강하게 오른(과열=기대감) 구간에 팔고, 눌린(공포)
구간에 산다. look-ahead 없이 ex-ante로 테스트 가능. 우량주 바구니에 적용해
'그냥 사서 보유(buy&hold)'를 비용 빼고 이기는지 본다.

+ 진단: 큰 하락갭(이벤트/실적 서프라이즈 프록시) '직전 N일 상승률'을 측정해
  "이벤트 전 기대감 상승 → 이벤트에 하락" 패턴이 실제로 있는지 본다(with-hindsight, 참고용).

규칙: RSI<BUY_RSI(눌림) 매수, RSI>SELL_RSI(과열) 매도. 다음날 시가 체결, 비용 차감.
Usage: python3.12 backtest_anticipation.py [pages]  (default 6)
"""
from __future__ import annotations
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import backtest as BT     # noqa: E402
import indicators as IND  # noqa: E402

BASKET = ["COST", "NVDA", "MSFT", "LLY", "AAPL", "AMZN", "GOOGL", "META", "V", "UNH"]
BUY_RSI, SELL_RSI = 40, 65
WARMUP = 40


def swing(rows):
    """RSI 역추세 스윙. (compounded 총수익, 거래수, 승수, net리스트)."""
    trades, mult, pos = [], 1.0, None
    for t in range(WARMUP, len(rows) - 1):
        ind = IND.compute(rows[:t + 1]); rsi = ind.get("rsi14")
        if rsi is None:
            continue
        nxt = float(rows[t + 1]["open"])
        if pos is None and rsi < BUY_RSI:
            pos = nxt * (1 + BT.LEG_COST)
        elif pos is not None and rsi > SELL_RSI:
            net = nxt / pos - 1 - BT.LEG_COST
            trades.append(net); mult *= (1 + net); pos = None
    if pos is not None:                       # 청산
        net = float(rows[-1]["close"]) / pos - 1 - BT.LEG_COST
        trades.append(net); mult *= (1 + net)
    return mult - 1, trades


def bh(rows):
    c0 = float(rows[WARMUP]["close"]); c1 = float(rows[-1]["close"])
    return c1 / c0 - 1 - 2 * BT.LEG_COST


def pre_event_drift(rows, k=5, look=5):
    """큰 하락갭 상위 k일의 '직전 look일 상승률' 평균 + 그날 갭 평균(진단)."""
    gaps = []
    for t in range(look + 1, len(rows)):
        try:
            g = float(rows[t]["open"]) / float(rows[t - 1]["close"]) - 1
            gaps.append((g, t))
        except Exception:
            pass
    downs = sorted(gaps)[:k]     # 가장 큰 하락갭
    if not downs:
        return None, None
    runups = [float(rows[t - 1]["close"]) / float(rows[t - 1 - look]["close"]) - 1 for _, t in downs]
    return (sum(runups) / len(runups) * 100, sum(g for g, _ in downs) / len(downs) * 100)


def run(pages=6):
    data = BT.load_all(pages)
    syms = [s for s in BASKET if s in data]
    print(f"검증 종목({len(syms)}): {', '.join(syms)}\n")
    print(f"{'종목':<7}{'스윙(역추세)':>14}{'매수보유':>12}{'거래':>7}{'승률':>8}  |  이벤트전{look_lbl()}상승 / 갭")
    print("-" * 78)
    win_swing = 0
    sw_tot = bh_tot = 0.0
    for s in syms:
        rows = data[s]["rows"]
        if len(rows) < WARMUP + 20:
            continue
        sret, trades = swing(rows)
        bret = bh(rows)
        wr = round(sum(1 for t in trades if t > 0) / len(trades) * 100, 0) if trades else 0
        drift, gap = pre_event_drift(rows)
        sw_tot += sret; bh_tot += bret
        win_swing += 1 if sret > bret else 0
        print(f"{s:<7}{sret*100:>+13.1f}%{bret*100:>+11.1f}%{len(trades):>7}{wr:>7.0f}%  |  "
              f"{('' if drift is None else f'{drift:+.1f}%'):>8} / {('' if gap is None else f'{gap:+.1f}%'):>7}")
    n = len(syms)
    print("-" * 78)
    print(f"평균     {sw_tot/n*100:>+13.1f}%{bh_tot/n*100:>+11.1f}%")
    print(f"\n스윙이 매수보유를 이긴 종목: {win_swing}/{n}")

    # ── 안정성 지표(스윙): 낙폭·최악손실·연속손실 ──
    print("\n[안정성 지표 — 역추세 스윙 vs 매수보유]")
    print(f"{'종목':<7}{'스윙 최대낙폭':>14}{'스윙 최악거래':>14}{'스윙 평균손실':>14}{'보유 최대낙폭':>14}")
    print("-" * 66)
    sdd_sum = bdd_sum = 0.0
    for s in syms:
        rows = data[s]["rows"]
        if len(rows) < WARMUP + 20:
            continue
        _, trades = swing(rows)
        # 스윙 equity 곡선(거래 순차 복리) → 최대낙폭
        eq, peak, sdd = 1.0, 1.0, 0.0
        for t in trades:
            eq *= (1 + t); peak = max(peak, eq); sdd = min(sdd, eq / peak - 1)
        worst = min(trades) * 100 if trades else 0
        losses = [t for t in trades if t <= 0]
        avgl = sum(losses) / len(losses) * 100 if losses else 0
        # 매수보유 최대낙폭
        closes = [float(r["close"]) for r in rows[WARMUP:]]
        bp, bdd = closes[0], 0.0
        for c in closes:
            bp = max(bp, c); bdd = min(bdd, c / bp - 1)
        sdd_sum += sdd * 100; bdd_sum += bdd * 100
        print(f"{s:<7}{sdd*100:>+13.1f}%{worst:>+13.1f}%{avgl:>+13.1f}%{bdd*100:>+13.1f}%")
    print("-" * 66)
    print(f"평균     {sdd_sum/n:>+13.1f}%{'':>14}{'':>14}{bdd_sum/n:>+13.1f}%")
    print("→ 스윙 최대낙폭이 보유보다 얕으면 = '안정성'은 실제로 스윙이 우위(수익은 낮아도).")
    print("\n판정:")
    if sw_tot > bh_tot:
        print("  ✅ '기대감에 팔고 눌림에 산다'가 평균적으로 매수보유 초과 — 통찰에 근거 있음")
    else:
        print("  ❌ 평균적으로 매수보유가 더 나음 — 강한 상승장에선 '강할 때 팔기'가 승자를 조기절단")
    print("진단(이벤트전 상승/갭): 값이 '양수 상승 → 음수 갭'이면 '기대감 상승 후 이벤트 하락' 패턴 존재.")
    print("주의: RSI 40/65 파라미터·비용 0.35%/leg·2022-26 강세장 편향. 과최적화 경계.")


def look_lbl():
    return "5일"


if __name__ == "__main__":
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 6)
