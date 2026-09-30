"""index_engine.py — evidence-based index investing engine (급등주 봇의 대체).

백테스트(2022-2026, 하락장 포함)가 유일하게 '돈 번다'고 판정한 방식만 자동화한다:
  · 코어      : 광범위 인덱스에 정액 적립(DCA). 기본 QQQ(나스닥100).
                더 분산·보수적이면 VOO(S&P500)로 CORE만 바꾸면 됨.
  · 추세 오버레이(선택): 지수가 200일선 위일 때만 보유, 아래면 현금.
                → 수익 조금↓ 대신 최대낙폭 절반(−23%→−14%). 폭락 방어.

이 엔진은 '신호 + 적립 플랜 + dry-run 주문'까지만 만든다. 실제 매수는 사람이
토스앱에서 하거나, preview→confirm으로 실행(월 1회라 수동으로 충분).

Usage:
  python3.12 index_engine.py            신호+플랜 리포트
  python3.12 index_engine.py --plan 60  $60 적립분 dry-run 주문 생성
"""
from __future__ import annotations
import sys

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S   # noqa: E402

CORE = "QQQ"              # 코어 인덱스 (보수적이면 "VOO"로)
MA_LONG = 200            # 추세 판정 이동평균
MONTHLY_KRW = None       # 월 적립액(원). None이면 사용자가 정함(권장: 감당 가능한 고정액)


def _hist(sym, need=210):
    """페이지네이션(nextBefore)으로 need봉 이상 확보 → (종가리스트 오름차순, 현재가)."""
    import time
    acc, before = {}, None
    for _ in range(3):
        r = None
        for _try in range(3):
            try:
                r = (S.CLIENT.get_candles(sym, "1d", 190, before=before)
                     if before else S.CLIENT.get_candles(sym, "1d", 190))
                break
            except Exception:
                time.sleep(1.2)
        if not r:
            break
        cs = r.get("candles", [])
        if not cs:
            break
        for c in cs:
            acc[str(c["timestamp"])[:10]] = float(c["close"])
        if len(acc) >= need:
            break
        before = r.get("nextBefore")
        if not before:
            break
    closes = [acc[k] for k in sorted(acc)]
    return closes, (closes[-1] if closes else None)


def trend(sym):
    closes, last = _hist(sym)
    if len(closes) < MA_LONG:
        return {"sym": sym, "ok": False}
    ma = sum(closes[-MA_LONG:]) / MA_LONG
    ma50 = sum(closes[-50:]) / 50
    return {"sym": sym, "ok": True, "last": last, "ma200": ma, "ma50": ma50,
            "above": last > ma, "gapPct": round((last / ma - 1) * 100, 1)}


def report():
    t = trend(CORE)
    print("=" * 60)
    print(f"인덱스 투자 엔진 — 코어 {CORE}")
    print("=" * 60)
    if not t["ok"]:
        print("데이터 부족(네트워크 재시도 필요)"); return
    sig = "🟢 IN (보유/적립)" if t["above"] else "🔴 OUT (현금 대기)"
    print(f"현재가 {t['last']:.2f} | 200일선 {t['ma200']:.2f} ({t['gapPct']:+}%) | 50일선 {t['ma50']:.2f}")
    print(f"추세 신호: {sig}")
    print()
    print("── 두 가지 운용법 (본인 성향대로 택1) ──")
    print("  A. 매수보유+적립(단순·최고수익): 신호 무시하고 매달 정액 적립, 계속 보유")
    print(f"  B. 추세추종(낙폭방어): 200일선 위({'현재 해당' if t['above'] else '현재 미해당'})일 때만 보유,")
    print("     200일선 아래로 '종가 이탈'하면 전량 현금 → 다시 위로 올라오면 재진입")
    print()
    try:
        usd = S.CLIENT.get_buying_power("USD").get("availableAmount")
        krw = S.CLIENT.get_buying_power("KRW").get("availableAmount")
        print(f"매수가능: USD ${usd} / KRW ₩{krw}")
    except Exception:
        pass
    print("\n권장: 감당 가능한 '월 정액'을 정해 매달 같은 날 적립. 떨어져도 계속(=싸게 사는 것).")
    print("추세추종을 원하면 이 엔진을 주 1회 돌려 IN/OUT 전환만 챙기면 됩니다.")


def plan(usd_amt):
    t = trend(CORE)
    if not t["ok"]:
        print("데이터 부족"); return
    if not t["above"]:
        print(f"⚠️ {CORE} 200일선 아래(추세 OUT). 추세추종이면 이번 적립은 현금 보류가 원칙.")
        print("   (매수보유+적립 방식이면 무시하고 적립해도 됨 — 본인 방식대로.)")
    r = S.t_plan_order(CORE, "BUY", "MARKET", order_amount=round(float(usd_amt), 2))
    import json
    if r.get("error"):
        print("plan:", json.dumps(r["error"], ensure_ascii=False)[:160])
    else:
        s = r.get("snapshot", {})
        print(f"DRY-RUN 적립 주문: {CORE} ${usd_amt} 시장가")
        print(f"  confirm 문구: {r.get('confirm_phrase')!r}")
        print(f"  (실행하려면 preview_order→confirm, 또는 토스앱에서 {CORE} ${usd_amt}어치 매수)")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--plan":
        plan(sys.argv[2])
    else:
        report()
