"""self_eval.py — 데이터 기반 자가평가 엔진(READ-ONLY).

시스템이 스스로 모은 데이터(원장·metrics·스크리너 히스토리·경보)를 읽어
룰별 성과/리스크/선별 적중률을 '임계값 기반 판정'으로 평가하고,
evals/eval_log.jsonl(기계용)+eval-<날짜>.md(사람용)로 적립한다.

판정 원칙: 표본이 임계(n) 미만이면 '표본부족—판단보류'(성급한 룰 변경 방지).
제안은 데이터가 임계를 넘을 때만 자동 생성. 주문/기록 변경 없음.

Usage: python3.12 self_eval.py        # 평가 실행+적립
       python3.12 self_eval.py --history   # 최근 평가 로그 5건
"""
import sys
import os
import json
from datetime import datetime
from pathlib import Path

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import execute as E        # noqa: E402
import stock_screener as SC  # noqa: E402

EVAL_DIR = Path(os.path.expanduser("~/.toss-trader/evals"))
MIN_N_RULE = 3       # 룰별 판정 최소 표본
MIN_N_PICKS = 8      # 스크리너 판정 최소 표본


def _rule_verdicts(trades):
    """청산 카테고리별 (n, 승률, 손익, 판정). 표본부족이면 판정 유보(순수함수)."""
    from collections import defaultdict
    by = defaultdict(list)
    for t in trades:
        by[t.get("exitCat") or "?"].append(t)
    out = {}
    for cat, ts in by.items():
        n = len(ts)
        win = sum(1 for t in ts if t["plUsd"] > 0)
        usd = sum(t["plUsd"] for t in ts)
        if n < MIN_N_RULE:
            v = "표본부족—유지"
        elif win / n >= 0.6 and usd > 0:
            v = "✅ 유효(돈 버는 룰)"
        elif usd < 0 and win / n < 0.4:
            v = "⚠️ 재검토 필요(지는 룰)"
        else:
            v = "중립—관찰 지속"
        out[cat] = {"n": n, "winRate": round(win / n * 100), "usd": round(usd, 2), "verdict": v}
    return out


def _screener_verdict(res):
    """픽 성과 → (적중률, 평균수익, 판정). res=[(sym,then,now,ret)]."""
    if not res:
        return None
    n = len(res)
    win = sum(1 for r in res if r[3] > 0)
    avg = sum(r[3] for r in res) / n
    if n < MIN_N_PICKS:
        v = "표본부족—적립 중"
    elif win / n >= 0.55 and avg > 0:
        v = "✅ 선별 유효"
    elif win / n < 0.45 or avg < -1:
        v = "⚠️ 가중치 재조정 후보(RS/눌림/변동성)"
    else:
        v = "중립"
    return {"n": n, "hitRate": round(win / n * 100), "avgRet": round(avg, 2), "verdict": v}


def _equity_trend(rows, window=40):
    """metrics.jsonl 최근 창의 미실현% 추세(첫→끝)와 변동폭."""
    vals = [float(r.get("unrealPct") or 0) for r in rows[-window:]]
    if len(vals) < 2:
        return None
    return {"from": vals[0], "to": vals[-1], "min": min(vals), "max": max(vals),
            "snapshots": len(vals)}


def run_eval():
    now = datetime.now()
    print("=" * 60)
    print(f"자가평가 (self_eval · READ-ONLY) — {now.strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    # ── 1. 실현 성과(룰별) ──
    trades = E._realized_trades()
    st = E._report_stats(trades)
    rules = _rule_verdicts(trades)
    print(f"\n[실현] {st['n'] if st else 0}건", end="")
    if st:
        print(f" | 승률 {st['winRate']:.0f}% | PF {st['pf']:.2f} | ${st['totalUsd']:+.2f} "
              f"| 기대값 ${st['expUsd']:+.2f} | MDD ${st['maxDD']:+.2f}")
    else:
        print(" — 없음")
    for cat, r in sorted(rules.items()):
        print(f"  {cat:9s} n={r['n']} 승률{r['winRate']}% ${r['usd']:+.2f} → {r['verdict']}")
    # ── 2. 리스크 이벤트 ──
    evs = E._read_journal()
    circuits = sum(1 for e in evs if e.get("event") == "BUY_CIRCUIT")
    alerts = sum(1 for e in evs if e.get("event") == "PORTFOLIO_ALERT")
    gaps = sum(1 for e in evs if e.get("event") == "GAP_THROUGH_STOP")
    halts = 1 if (Path.home() / ".toss-mcp" / "HALT").exists() else 0
    print(f"\n[리스크] 서킷차단 {circuits} | 포트경보 {alerts} | 갭관통 {gaps} | 현재HALT {halts}")
    risk_v = "✅ 리스크 이벤트 정상 범위" if (gaps == 0 and halts == 0) else "⚠️ 리스크 이벤트 발생 — 리뷰"
    print(f"  → {risk_v}")
    # ── 3. 에쿼티 추세 ──
    mrows = []
    try:
        for line in (Path(os.path.expanduser("~/.toss-trader")) / "metrics.jsonl").open(encoding="utf-8"):
            try:
                mrows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    eq = _equity_trend(mrows)
    if eq:
        print(f"\n[미실현 추세] {eq['snapshots']}스냅샷: {eq['from']:+.1f}% → {eq['to']:+.1f}% "
              f"(범위 {eq['min']:+.1f}~{eq['max']:+.1f}%)")
    # ── 4. 스크리너 적중률 ──
    hist = []
    try:
        for line in open(os.path.expanduser("~/.toss-trader/screener_last_history.jsonl"), encoding="utf-8"):
            try:
                hist.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    picks = None
    if hist:
        syms = {t["sym"] for h in hist for t in (h.get("top") or [])}
        prices = {}
        for s in sorted(syms):
            try:
                prices[s] = float(E.S.CLIENT.get_price(s)["lastPrice"])
            except Exception:
                continue
        res, ts0 = SC._review_picks(hist, prices, min_age_days=3)
        picks = _screener_verdict(res)
        if picks:
            print(f"\n[스크리너] 픽 {picks['n']}개(기준 {str(ts0)[:10]}) 적중 {picks['hitRate']}% "
                  f"평균 {picks['avgRet']:+.2f}% → {picks['verdict']}")
    # ── 5. 자동 제안(임계 충족 시에만) ──
    suggestions = []
    for cat, r in rules.items():
        if "재검토" in r["verdict"]:
            suggestions.append(f"{cat} 룰이 n={r['n']}서 지는 중(${r['usd']:+.2f}) — 파라미터 리뷰 후보")
    if picks and "재조정" in picks["verdict"]:
        suggestions.append(f"스크리너 적중률 {picks['hitRate']}%(n={picks['n']}) — 가중치 백테스트 착수 후보")
    if st and st["n"] >= 10 and st["pf"] < 1.2:
        suggestions.append(f"전체 PF {st['pf']:.2f}<1.2 — 전략 전반 리뷰")
    print("\n[제안]")
    if suggestions:
        for s in suggestions:
            print(f"  🔧 {s}")
    else:
        print("  없음 — 현행 유지(모든 판정이 유효/중립/표본부족)")
    # ── 적립 ──
    rec = {"ts": now.isoformat(timespec="seconds"),
           "realized": st, "rules": rules, "risk": {"circuits": circuits, "alerts": alerts,
                                                     "gaps": gaps, "halt": halts},
           "equity": eq, "picks": picks, "suggestions": suggestions}
    try:
        EVAL_DIR.mkdir(parents=True, exist_ok=True)
        with (EVAL_DIR / "eval_log.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        md = EVAL_DIR / f"eval-{now.strftime('%Y%m%d')}.md"
        md.write_text(
            f"# 자가평가 {now.strftime('%Y-%m-%d %H:%M')}\n\n"
            + (f"- 실현 {st['n']}건 승률 {st['winRate']:.0f}% PF {st['pf']:.2f} ${st['totalUsd']:+.2f}\n" if st else "- 실현 없음\n")
            + "".join(f"- {c}: n={r['n']} 승률{r['winRate']}% ${r['usd']:+.2f} → {r['verdict']}\n" for c, r in sorted(rules.items()))
            + (f"- 스크리너: 적중 {picks['hitRate']}%(n={picks['n']}) → {picks['verdict']}\n" if picks else "")
            + f"- 리스크: 서킷{circuits}/경보{alerts}/갭{gaps}/HALT{halts} → {risk_v}\n"
            + ("- 제안: " + " | ".join(suggestions) + "\n" if suggestions else "- 제안 없음(현행 유지)\n"),
            encoding="utf-8")
        print(f"\n적립: {md}")
    except Exception as e:
        print("적립 실패:", e)
    return 0


def show_history(n=5):
    try:
        rows = [json.loads(x) for x in
                (EVAL_DIR / "eval_log.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    except Exception:
        print("평가 이력 없음"); return 0
    for r in rows[-n:]:
        st = r.get("realized") or {}
        print(f"[{str(r.get('ts'))[:16]}] 실현{st.get('n', 0)}건 ${st.get('totalUsd', 0):+.2f} "
              f"| 제안 {len(r.get('suggestions') or [])}건")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--history":
        sys.exit(show_history())
    sys.exit(run_eval())
