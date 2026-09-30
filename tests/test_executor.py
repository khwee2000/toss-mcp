"""test_executor.py — live_trade.py REAL-order lifecycle regressions (round 2).

Locks in fixes verified against the official Toss OrderStatus enum
(PENDING/PARTIAL_FILLED/FILLED/CANCELED/REJECTED/REPLACED/…). _poll_fill now
returns a 5-tuple (status, fq, ap, outcome, cost) with outcome ∈
{filled, partial_terminal, dead, working}. Isolated: mock transport, order
calls + _poll_fill stubbed, state/journal/marker in a temp dir. No network.

Run: python3.12 tests/test_executor.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["TOSS_LIVE"] = "0"
os.environ["TOSS_CLIENT_ID"] = ""
os.environ["TOSS_CLIENT_SECRET"] = ""
os.environ["TOSS_ALLOW_LIVE_ORDERS"] = "1"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as S            # noqa: E402
import market_cycle as MC     # noqa: E402
import live_trade as LT       # noqa: E402

_TMP = Path(tempfile.mkdtemp())
MC.STATE = _TMP / "paper.json"
MC.JOURNAL = _TMP / "journal.jsonl"
LT.GO_MARKER = _TMP / "LIVE_GO"
LT.GO_MARKER.write_text("go")
LT.POLL_TRIES, LT.POLL_WAIT = 1, 0.0

PASS = FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {label}")
    else:
        FAIL += 1
        print(f"  FAIL: {label}")


def seed(positions, cash=1.0, entries=0):
    MC.save_state({"positions": positions, "cashFrac": cash, "realizedPlPct": 0.0,
                   "dailyStats": {"date": "2026-07-14", "entries": entries, "lossCloses": 0},
                   "weekStats": {"week": "x", "entries": entries}})


def realpos(**kw):
    p = {"symbol": "AMD", "entryRef": 100.0, "weight": 0.5, "entryDate": "2026-07-14",
         "highwater": 100.0, "tp1_done": False, "real": True, "orderId": "O1",
         "qty": 0.4, "fx": 1495.0, "entryCostUsd": 0.0, "pendingFill": False}
    p.update(kw)
    return p


def stub(poll):
    S.t_plan_order = lambda *a, **k: {"preview_token": "t", "confirm_phrase": "x",
                                      "snapshot": {"snapshotPrice": 100.0}}
    S.t_place_order_confirmed = lambda *a, **k: {"orderId": "OID"}
    S.CLIENT.get_exchange_rate = lambda *a, **k: {"rate": "1495"}
    LT._poll_fill = lambda oid: poll                      # 5-tuple
    LT.preflight = lambda st, sym, usd: (True, usd, [])
    MC.us_session = lambda now=None: "미국 정규장"


def test_enter_rejected_no_phantom():
    print("\n[enter] REJECTED → 유령 포지션·현금·카운터 없음")
    seed([]); stub(("REJECTED", 0.0, None, "dead", 0.0))
    rc = LT.cmd_enter("AMD", 45)
    st = MC.load_state()
    check(rc == 1 and not st["positions"], "포지션 미기록")
    check(st["cashFrac"] == 1.0 and st["dailyStats"]["entries"] == 0, "현금·카운터 불변")


def test_enter_filled_books_actual():
    print("\n[enter] FILLED → 실체결 qty/price/weight")
    seed([]); stub(("FILLED", 0.4, 110.0, "filled", 0.0))
    rc = LT.cmd_enter("AMD", 45)
    p = MC.load_state()["positions"][0]
    exp_w = round(0.4 * 110.0 * 1495 / LT.REAL_CAPITAL_KRW, 4)
    check(rc == 0 and p["qty"] == 0.4 and p["entryRef"] == 110.0 and not p["pendingFill"],
          "FILLED 활성 포지션")
    check(abs(p["weight"] - exp_w) < 1e-6, f"weight=실제노셔널 ({p['weight']})")


def test_enter_working_partial_pending():
    print("\n[enter] PARTIAL_FILLED(working) → 실체결분 + pendingFill")
    seed([]); stub(("PARTIAL_FILLED", 0.2, 110.0, "working", 0.0))
    rc = LT.cmd_enter("AMD", 45)
    p = MC.load_state()["positions"][0]
    check(rc == 0 and p["qty"] == 0.2 and p["pendingFill"], "부분(진행중) pending")


def test_enter_partial_terminal_active():
    print("\n[enter] CANCELED+부분체결(terminal) → 활성 포지션(pending 아님)")
    seed([]); stub(("CANCELED", 0.2, 110.0, "partial_terminal", 0.0))
    rc = LT.cmd_enter("AMD", 45)
    p = MC.load_state()["positions"][0]
    check(rc == 0 and p["qty"] == 0.2 and not p["pendingFill"], "종결부분 → 활성")


def test_exit_rejected_keeps_position():
    print("\n[exit] REJECTED → 포지션 유지·손익 미계상")
    seed([realpos()]); stub(("REJECTED", 0.0, None, "dead", 0.0))
    rc = LT.cmd_exit("AMD", "STOP")
    st = MC.load_state()
    check(rc == 1 and len(st["positions"]) == 1 and st["realizedPlPct"] == 0.0, "유지+무계상")


def test_exit_working_no_mutation():
    print("\n[exit] 매도 아직 진행중(working, 부분체결) → 상태 절대 불변 (R3)")
    seed([realpos()]); stub(("PARTIAL_FILLED", 0.2, 120.0, "working", 0.0))
    rc = LT.cmd_exit("AMD", "STOP")
    st = MC.load_state()
    p = st["positions"][0]
    check(rc == 1 and len(st["positions"]) == 1, "포지션 유지")
    check(p["qty"] == 0.4 and st["realizedPlPct"] == 0.0, "수량·실현 불변(이중계상 없음)")


def test_exit_full_filled():
    print("\n[exit] 전량 FILLED → 청산·실현")
    seed([realpos(entryRef=100.0, qty=0.4, weight=0.5)])
    stub(("FILLED", 0.4, 110.0, "filled", 0.0))
    rc = LT.cmd_exit("AMD", "TP2_REST")
    st = MC.load_state()
    check(rc == 0 and not st["positions"], "포지션 제거")
    check(abs(st["realizedPlPct"] - 5.0) < 1e-6, f"실현 +5.0pt ({st['realizedPlPct']})")


def test_exit_partial_terminal_reduces():
    print("\n[exit] 부분매도(terminal) → 실제 매도분만, 잔량 유지")
    seed([realpos(entryRef=100.0, qty=0.4, weight=0.5)])
    stub(("CANCELED", 0.2, 110.0, "partial_terminal", 0.0))
    rc = LT.cmd_exit("AMD", "TP2_REST")
    st = MC.load_state(); p = st["positions"][0]
    check(rc == 0 and abs(p["qty"] - 0.2) < 1e-9, f"잔량 0.2 ({p['qty']})")
    check(abs(st["realizedPlPct"] - 2.5) < 1e-6, f"실현 2.5pt(실매도분) ({st['realizedPlPct']})")


def test_exit_cost_drag():
    print("\n[exit] 수수료·세금 → 실현 P/L에서 차감 (N4)")
    # entryCost $0.2 + exit cost $0.2 on 0.4주 @ entry 100 = notional $40
    seed([realpos(entryRef=100.0, qty=0.4, weight=0.5, entryCostUsd=0.2)])
    stub(("FILLED", 0.4, 110.0, "filled", 0.2))
    rc = LT.cmd_exit("AMD", "TP2_REST")
    st = MC.load_state()
    # cost_pct = (0.2+0.2)/(0.4*100)*100 = 1.0% ; net_plp = 10-1 = 9 ; gain = 0.5*9 = 4.5
    check(rc == 0 and abs(st["realizedPlPct"] - 4.5) < 1e-6,
          f"비용 반영 실현 +4.5pt(순) ({st['realizedPlPct']})")


def test_exit_tp1_threshold():
    print("\n[exit] TP1 소량 부분체결 → tp1_done 미설정 (N6)")
    # qty 0.4, TP1 sells 0.2; but only 0.02 (10%) fills terminally
    seed([realpos(entryRef=100.0, qty=0.4, weight=0.5)])
    stub(("CANCELED", 0.02, 110.0, "partial_terminal", 0.0))
    LT.cmd_exit("AMD", "TP1_HALF")
    p = MC.load_state()["positions"][0]
    check(not p["tp1_done"], "소량 부분체결은 tp1_done=False (추가 TP1 가능)")


def test_status_reconcile_dead_refunds_counter():
    print("\n[status] pending→dead → 제거 + 현금·진입카운터 환원 (N5)")
    seed([realpos(pendingFill=True, weight=0.5, qty=0.0, entryRef=None)],
         cash=0.5, entries=1)
    stub(("CANCELED", 0.0, None, "dead", 0.0))
    rc = LT.cmd_status()
    st = MC.load_state()
    check(rc == 0 and not st["positions"], "유령 제거")
    check(abs(st["cashFrac"] - 1.0) < 1e-6, "현금 환원")
    check(st["dailyStats"]["entries"] == 0 and st["weekStats"]["entries"] == 0,
          "진입 카운터 환불")


def test_status_reconcile_terminal_partial_activates():
    print("\n[status] pending→terminal 부분체결 → 활성화(pendingFill False) (R1)")
    seed([realpos(pendingFill=True, weight=0.0, qty=0.0, entryRef=None)], cash=1.0)
    stub(("CANCELED", 0.3, 120.0, "partial_terminal", 0.0))
    rc = LT.cmd_status()
    p = MC.load_state()["positions"][0]
    check(rc == 0 and p["qty"] == 0.3 and p["entryRef"] == 120.0 and not p["pendingFill"],
          "종결부분 → 관리대상 활성(고아 아님)")


def test_status_working_stays_pending():
    print("\n[status] pending→working → 계속 대기")
    seed([realpos(pendingFill=True, weight=0.0, qty=0.0, entryRef=None)], cash=1.0)
    stub(("PENDING", 0.0, None, "working", 0.0))
    LT.cmd_status()
    p = MC.load_state()["positions"][0]
    check(p["pendingFill"], "working → pendingFill 유지")


def test_open_contrib_guard():
    print("\n[cycle] _open_contrib: pending/entryRef=None 안전(R2)")
    st = {"positions": [{"symbol": "X", "entryRef": None, "weight": 0.5, "pendingFill": True},
                        {"symbol": "Y", "entryRef": 0, "weight": 0.5}]}
    ok = True
    try:
        MC._open_contrib(st)      # must NOT raise
    except Exception:
        ok = False
    check(ok, "None/0 entryRef에서 크래시 없음")


def test_lock_failclosed():
    print("\n[lock] 상호배제 + 획득실패 None")
    lk1 = MC._acquire_lock(block=False)
    check(lk1 is not None, "1차 획득")
    check(MC._acquire_lock(block=False) is None, "보유 중 2차 → None(가짜핸들 아님)")
    lk1.close()
    lk3 = MC._acquire_lock(block=True, timeout=1.0)
    check(lk3 is not None, "해제 후 재획득")
    lk3.close()


if __name__ == "__main__":
    test_enter_rejected_no_phantom()
    test_enter_filled_books_actual()
    test_enter_working_partial_pending()
    test_enter_partial_terminal_active()
    test_exit_rejected_keeps_position()
    test_exit_working_no_mutation()
    test_exit_full_filled()
    test_exit_partial_terminal_reduces()
    test_exit_cost_drag()
    test_exit_tp1_threshold()
    test_status_reconcile_dead_refunds_counter()
    test_status_reconcile_terminal_partial_activates()
    test_status_working_stays_pending()
    test_open_contrib_guard()
    test_lock_failclosed()
    print(f"\n=== executor: {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)
