"""test_harness.py — trading-harness regression suite (v2).

Locks in the audit fixes: signal priority (trail/stop/trend/TP/time), state-v2
migration, realized-P&L accounting via cmd_signal, day-anchored circuit math,
risk-manager bucket/cash guards, and --apply validation.

Forced MOCK (env set before imports). Pure/deterministic: network functions are
either injected (evaluate_position) or monkeypatched (price_meta).

Run: python3.12 tests/test_harness.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

os.environ["TOSS_LIVE"] = "0"
os.environ["TOSS_CLIENT_ID"] = ""
os.environ["TOSS_CLIENT_SECRET"] = ""
os.environ["TOSS_ALLOW_LIVE_ORDERS"] = "0"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import market_cycle as MC   # noqa: E402
import risk_manager as RM   # noqa: E402

# isolate state/journal into a temp dir
_TMP = Path(tempfile.mkdtemp())
MC.STATE = _TMP / "paper_portfolio.json"
MC.JOURNAL = _TMP / "journal.jsonl"

PASS = FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {label}")
    else:
        FAIL += 1
        print(f"  FAIL: {label}")


def _pos(**kw):
    p = {"symbol": "TST", "entryRef": 100.0, "weight": 0.2,
         "entryDate": "2026-07-14", "highwater": 100.0, "tp1_done": False}
    p.update(kw)
    return p


def test_migration():
    print("\n[state] v1 -> v2 migration")
    MC.STATE.write_text(json.dumps({
        "positions": [{"symbol": "AMD", "entryRef": 544, "weight": 0.2, "tp1_done": False}],
        "cashFrac": 0.8}))
    st = MC.load_state()
    p = st["positions"][0]
    check(p.get("entryDate") and p.get("highwater") == 544, "entryDate+highwater 채워짐")
    check(st["version"] == MC.STATE_VERSION and "realizedPlPct" in st
          and "peakEquityPct" in st and "cooldowns" in st and "dailyStats" in st,
          f"v{MC.STATE_VERSION} 필드(realized/peak/cooldowns/dailyStats) 추가")


def test_signal_priority():
    print("\n[signals] priority: trail/stop > trend > TP1 > TP2 > time > hold")
    D = date(2026, 7, 14)
    s_up = {"ma20": 90.0}     # price above MA20 unless stated
    # 1) plain -8% -> STOP (init stop, not locked)
    e = MC.evaluate_position(_pos(), cur=92.0, s=s_up, today=D)
    check(e["signal"] == "STOP", f"-8% -> STOP ({e['signal']})")
    # 2) trailing lock: hw 120, cur 109 -> stop 110.4 above entry -> TRAIL_STOP
    e = MC.evaluate_position(_pos(highwater=120.0), cur=109.0, s=s_up, today=D)
    check(e["signal"] == "TRAIL_STOP", f"고점후 되돌림 -> TRAIL_STOP ({e['signal']})")
    # 3) trend exit: above stop but below MA20
    e = MC.evaluate_position(_pos(highwater=104.0), cur=101.0, s={"ma20": 102.0}, today=D)
    check(e["signal"] == "TREND_EXIT", f"MA20 이탈 -> TREND_EXIT ({e['signal']})")
    # 4) TP1 at +6%
    e = MC.evaluate_position(_pos(highwater=106.0), cur=106.0, s=s_up, today=D)
    check(e["signal"] == "TP1_HALF", f"+6% -> TP1_HALF ({e['signal']})")
    # 5) TP2 at +11% (tp1 done)
    e = MC.evaluate_position(_pos(highwater=111.0, tp1_done=True), cur=111.0, s=s_up, today=D)
    check(e["signal"] == "TP2_REST", f"+11% -> TP2_REST ({e['signal']})")
    # 6) time exit: 30 days, +1%
    e = MC.evaluate_position(_pos(entryDate="2026-06-14", highwater=101.0),
                             cur=101.0, s=s_up, today=D)
    check(e["signal"] == "TIME_EXIT", f"30일 무진전 -> TIME_EXIT ({e['signal']})")
    # 7) hold + highwater ratchets up
    e = MC.evaluate_position(_pos(), cur=103.0, s=s_up, today=D)
    check(e["signal"] == "HOLD" and e["highwater"] == 103.0,
          f"+3% -> HOLD, highwater 103 ({e['signal']},{e['highwater']})")


def test_apply_signal_accounting():
    print("\n[accounting] TRIM/CLOSE update realizedPlPct + cash + journal")
    MC.STATE.write_text(json.dumps({"positions": [
        {"symbol": "AMD", "entryRef": 100.0, "weight": 0.2,
         "entryDate": "2026-07-14", "highwater": 100.0, "tp1_done": False}],
        "cashFrac": 0.8}))
    if MC.JOURNAL.exists():
        MC.JOURNAL.unlink()
    orig = MC.price_meta
    orig_sp = MC.spread_pct
    orig_fee = MC.FEE_PCT_US
    MC.price_meta = lambda s: (106.0, 5)      # deterministic +6%
    MC.spread_pct = lambda s: 0.0             # no sell slippage in this test
    MC.FEE_PCT_US = 0.0                       # no fee in this determinism test
    try:
        rc = MC.cmd_signal("AMD", "TP1_HALF")
        st = MC.load_state()
        check(rc == 0 and abs(st["realizedPlPct"] - 0.6) < 1e-6,
              f"TP1: 실현 +0.6pt (0.1w x 6%) ({st['realizedPlPct']})")
        check(abs(st["positions"][0]["weight"] - 0.1) < 1e-9 and st["positions"][0]["tp1_done"],
              "TP1: weight 절반 + tp1_done")
        check(abs(st["cashFrac"] - 0.9) < 1e-9, f"TP1: 현금 0.9 ({st['cashFrac']})")
        MC.price_meta = lambda s: (95.0, 5)   # then closes at -5%
        rc = MC.cmd_signal("AMD", "STOP")
        st = MC.load_state()
        check(rc == 0 and abs(st["realizedPlPct"] - 0.1) < 1e-6,
              f"CLOSE: 실현 0.6-0.5=+0.1pt ({st['realizedPlPct']})")
        check(not st["positions"] and abs(st["cashFrac"] - 1.0) < 1e-9,
              "CLOSE: 포지션 제거 + 현금 1.0")
        lines = [json.loads(x) for x in MC.JOURNAL.read_text().splitlines()]
        check([d["event"] for d in lines] == ["TRIM", "CLOSE"], "저널 TRIM->CLOSE 기록")
        check(MC.cmd_signal("AMD", "STOP") == 1, "미보유 시그널 거부")
    finally:
        MC.price_meta = orig
        MC.spread_pct = orig_sp
        MC.FEE_PCT_US = orig_fee


def test_circuit_math():
    print("\n[circuit] day-anchored daily loss + peak drawdown")
    st = {"realizedPlPct": 0.0, "peakEquityPct": 10.0,
          "dayStart": {"date": "2026-07-14", "equity": 2.0}}
    eq, daily, dd, ok, why = MC.equity_and_circuit(st, -6.0, "2026-07-14")
    check(eq == -6.0 and daily == -8.0 and dd == -16.0, f"eq/daily/dd = {eq}/{daily}/{dd}")
    check(not ok, "낙폭 -16 -> 서킷 TRIP")
    st2 = {"realizedPlPct": 1.0, "peakEquityPct": 2.0,
           "dayStart": {"date": "2026-07-13", "equity": 5.0}}
    eq, daily, dd, ok, _ = MC.equity_and_circuit(st2, 1.0, "2026-07-14")
    check(daily == 0.0 and ok, f"날짜 바뀌면 dayStart 재앵커 (daily={daily})")


def test_risk_guards():
    print("\n[risk] bucket own-symbol + true min-cash case")
    ok, why = RM.check_new_entry(
        [{"symbol": "ZZ1", "weight": 0.2}, {"symbol": "ZZ2", "weight": 0.2}],
        0.6, "ZZ3", 0.2, 1.0)
    check(ok, f"미지정 종목끼리 허위 버킷집중 없음 ({why})")
    ok, why = RM.check_new_entry(
        [{"symbol": "ZZ1", "weight": 0.2}, {"symbol": "ZZ2", "weight": 0.2},
         {"symbol": "ZZ3", "weight": 0.2}], 0.4, "ZZ4", 0.21, 1.0)
    check(not ok and "현금" in why, f"현금예비 20% 가드 ({why})")


def test_apply_validation():
    print("\n[apply] state validation")
    check(MC.validate_state({"positions": [{"symbol": "A"}], "cashFrac": 0.8}) != [],
          "필수키 누락 -> 거부")
    check(MC.validate_state({"positions": [
        {"symbol": "A", "entryRef": 1, "weight": 0.5}], "cashFrac": 0.1}) != [],
          "비중합 0.6 -> 거부")
    check(MC.validate_state({"positions": [
        {"symbol": "A", "entryRef": 1, "weight": 0.2}], "cashFrac": 0.8}) == [],
          "정상 상태 -> 통과")


def test_v3_behavior_guards():
    print("\n[v3 guards] cooldown / daily limits / circuit / FOMO / session / weight")
    today = MC.datetime.now(MC.KST).strftime("%Y-%m-%d")
    tmr = (MC.datetime.now(MC.KST) + MC.timedelta(days=1)).strftime("%Y-%m-%d")
    base = {"positions": [], "cashFrac": 1.0}
    orig = (MC.price_meta, MC.spread_pct, MC.us_session, MC.snap, MC.equity_and_circuit)
    orig_fee = MC.FEE_PCT_US
    MC.price_meta = lambda s: (100.0, 5)
    MC.spread_pct = lambda s: 1.0
    MC.us_session = lambda now=None: "미국 정규장"
    MC.snap = lambda s: {"changePct": 1.0}
    MC.equity_and_circuit = lambda st, oc, t: (0, 0, 0, True, "OK")
    MC.FEE_PCT_US = 0.0
    try:
        MC.STATE.write_text(json.dumps(base))
        check(MC.cmd_enter("NVDA", 0.30) == 1, "#18 비중 0.30 거부")
        MC.STATE.write_text(json.dumps({**base, "cooldowns": {"NVDA": tmr}}))
        check(MC.cmd_enter("NVDA", 0.2) == 1, "#13 쿨다운 중 재진입 거부")
        MC.STATE.write_text(json.dumps(
            {**base, "dailyStats": {"date": today, "entries": 3, "lossCloses": 0}}))
        check(MC.cmd_enter("NVDA", 0.2) == 1, "#15 일일 진입한도 거부")
        MC.STATE.write_text(json.dumps(
            {**base, "dailyStats": {"date": today, "entries": 0, "lossCloses": 2}}))
        check(MC.cmd_enter("NVDA", 0.2) == 1, "#14 연속손실 후 신규 금지")
        MC.STATE.write_text(json.dumps(base))
        MC.equity_and_circuit = lambda st, oc, t: (0, -6, -16, False, "TRIP")
        check(MC.cmd_enter("NVDA", 0.2) == 1, "#17 서킷 TRIP 시 진입 거부")
        MC.equity_and_circuit = lambda st, oc, t: (0, 0, 0, True, "OK")
        MC.snap = lambda s: {"changePct": 9.5}
        check(MC.cmd_enter("NVDA", 0.2) == 1, "#16 FOMO(+9.5%) 추격 거부")
        MC.snap = lambda s: {"changePct": 1.0}
        MC.us_session = lambda now=None: "미국장 휴장"
        check(MC.cmd_enter("NVDA", 0.2) == 1, "#19 휴장 중 진입 거부")
        MC.us_session = lambda now=None: "미국 정규장"
        rc = MC.cmd_enter("NVDA", 0.2)
        st = MC.load_state()
        check(rc == 0 and st["positions"], "정상 진입 성공")
        # #25 slippage: entry = 100 * (1 + 1%/2/100) = 100.5
        check(abs(st["positions"][0]["entryRef"] - 100.5) < 1e-6,
              f"#25 진입 슬리피지 반영 (100→{st['positions'][0]['entryRef']})")
        check(st["dailyStats"]["entries"] == 1, "일일 진입 카운터 증가")
        # loss close arms cooldown + counts lossCloses
        MC.price_meta = lambda s: (93.0, 5)
        MC.spread_pct = lambda s: 0.0
        check(MC.cmd_signal("NVDA", "STOP") == 0, "손절 청산 실행")
        st = MC.load_state()
        check(st["cooldowns"].get("NVDA", "") > today, "#13 손절 후 쿨다운 등록")
        check(st["dailyStats"]["lossCloses"] == 1, "#14 손실청산 카운터 증가")
        check(st["weekStats"]["entries"] == 1, "주간 진입 카운터 증가")
        # --- 10선 추가 가드 ---
        MC.price_meta = lambda s: (100.0, 5)
        MC.spread_pct = lambda s: 0.0
        MC.FEE_PCT_US = 0.1
        check(MC.cmd_enter("MSFT", 0.2) == 0, "수수료 테스트 진입")
        st = MC.load_state()
        check(abs(st["positions"][0]["entryRef"] - 100.1) < 1e-6,
              f"수수료 0.1% 반영 (100→{st['positions'][0]['entryRef']})")
        check(MC.cmd_enter("MSFT", 0.2) == 1, "물타기 금지: 보유종목 재진입 거부")
        # 상관 가드: 동일 수익률 시계열 → corr 1.0 → 거부
        orig_r20 = MC._returns20
        MC._returns20 = lambda s: [0.01, -0.02, 0.015] * 7
        check(MC.cmd_enter("GOOGL", 0.2) == 1, "상관 가드: corr 1.0 거부")
        MC._returns20 = orig_r20
        # 주간 상한
        st = MC.load_state(); st["weekStats"]["entries"] = 8
        MC.save_state(st)
        check(MC.cmd_enter("AMZN", 0.2) == 1, "주간 매매횟수 상한 거부")
    finally:
        (MC.price_meta, MC.spread_pct, MC.us_session, MC.snap, MC.equity_and_circuit) = orig
        MC.FEE_PCT_US = orig_fee


def test_checksum_integrity():
    print("\n[#39] 상태 체크섬 무결성")
    MC.STATE.write_text(json.dumps({"positions": [], "cashFrac": 1.0}))
    st = MC.load_state()
    check(st["_integrity"] == "ok", "체크섬 없는 구상태 -> ok(경고 없음)")
    MC.save_state(st)
    check(MC.load_state()["_integrity"] == "ok", "저장 직후 무결성 ok")
    raw = MC.STATE.read_text().replace('"cashFrac": 1.0', '"cashFrac": 0.123')
    MC.STATE.write_text(raw)                       # 수동 오염 시뮬레이션
    check(MC.load_state()["_integrity"] == "MISMATCH", "오염 감지 -> MISMATCH")


def test_hysteresis_and_report():
    print("\n[v3] regime hysteresis + report stats + undo")
    st = {"lastRegime": "risk_off", "regimeStreak": {"regime": None, "count": 0}}
    check(MC.effective_regime(st, "neutral") == "risk_off", "#3 1회차 전환 보류")
    check(MC.effective_regime(st, "neutral") == "neutral", "#3 2연속 → 전환 확정")
    st2 = {"lastRegime": None, "regimeStreak": {"regime": None, "count": 0}}
    check(MC.effective_regime(st2, "risk_on") == "risk_on", "#3 최초 판정은 즉시")
    # report from crafted journal
    if MC.JOURNAL.exists():
        MC.JOURNAL.unlink()
    for ev in [{"event": "CLOSE", "realized": 2.0, "signal": "TP2_REST"},
               {"event": "CLOSE", "realized": -1.0, "signal": "STOP"},
               {"event": "TRIM", "realized": 0.5}]:
        MC.journal(ev)
    r = MC.report_stats()
    check(r["closes"] == 2 and r["wins"] == 1 and r["trims"] == 1, "#43 리포트 집계")
    check(abs(r["profitFactor"] - 2.0) < 1e-9 and abs(r["expectancy"] - 0.5) < 1e-9,
          f"#43 PF=2.0, 기대값=0.5 ({r['profitFactor']},{r['expectancy']})")
    check(r["bySignal"]["STOP"]["pl"] == -1.0 and r["bySignal"]["TP2_REST"]["pl"] == 2.0,
          "#49 시그널별 성과 분해")
    # undo restores previous state
    MC.STATE.write_text(json.dumps({"positions": [], "cashFrac": 1.0}))
    st_a = MC.load_state(); MC.save_state(st_a)          # snapshot A (creates .bak next)
    st_a["cashFrac"] = 0.5; MC.save_state(st_a)          # mutate -> .bak holds previous
    check(MC.cmd_undo() == 0, "#34 undo 실행")
    check(MC.load_state()["cashFrac"] == 1.0, "#34 직전 상태 복원")
    # journal rotation
    old_max = MC.JOURNAL_MAX_BYTES
    MC.JOURNAL_MAX_BYTES = 10
    try:
        MC.journal({"event": "X", "pad": "y" * 100})
        MC.journal({"event": "Y"})
        rotated = list(MC.JOURNAL.parent.glob("journal-*.jsonl"))
        check(len(rotated) >= 1, "#35 저널 로테이션")
    finally:
        MC.JOURNAL_MAX_BYTES = old_max


def test_redaction():
    print("\n[#54] redaction regex")
    import logging
    from toss_client import _RedactingFilter
    f = _RedactingFilter()
    rec = logging.LogRecord("x", 20, "", 0,
                            "auth Bearer eyJabcdefghijk.payload fail tssk_live_SECRET12345 done",
                            None, None)
    f.filter(rec)
    out = rec.getMessage()
    check("eyJabcdefghijk" not in out and "tssk_live_SECRET12345" not in out,
          "토큰/키 마스킹")
    check("done" in out and "auth" in out, "비밀 아닌 문맥은 보존")


if __name__ == "__main__":
    test_migration()
    test_signal_priority()
    test_apply_signal_accounting()
    test_circuit_math()
    test_risk_guards()
    test_apply_validation()
    test_v3_behavior_guards()
    test_hysteresis_and_report()
    test_checksum_integrity()
    test_redaction()
    print(f"\n=== harness: {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)
