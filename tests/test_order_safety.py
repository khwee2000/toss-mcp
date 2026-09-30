"""test_order_safety.py — Order PLAN/EXECUTE-gate safety regressions.

Locks in the stage-3 hardening (no live orders, no network):
  - PLAN positivity guards (quantity/orderAmount/price > 0; US round-to-0).
  - Execute gate: currency fail-closed, estimated-FX refusal (live USD),
    price-drift (MARKET/amount only) + fail-closed when price unverifiable,
    LIMIT not over-blocked.
  - Order POST body whitelist + numeric->string coercion.

Forces MOCK transport via env (set BEFORE importing server). Live-only gate
branches are exercised by flipping the frozen CFG.live with object.__setattr__
and stubbing CLIENT reads — no order ever reaches a network.

Run: python3.12 tests/test_order_safety.py   (no pytest dependency)
"""
from __future__ import annotations

import os
import sys

os.environ["TOSS_LIVE"] = "0"
os.environ["TOSS_CLIENT_ID"] = ""
os.environ["TOSS_CLIENT_SECRET"] = ""
os.environ["TOSS_ALLOW_LIVE_ORDERS"] = "1"   # register write tools (transport stays mock)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as S  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, label):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {label}")
    else:
        FAIL += 1
        print(f"  FAIL: {label}")


def _err_code(r):
    return (r or {}).get("error", {}).get("code") if isinstance(r, dict) else None


def test_positivity():
    print("\n[plan] positivity guards — no zero/negative size/price reaches a POST")
    check(_err_code(S.t_plan_order("005930", "BUY", "MARKET", quantity=0)) == "invalid-quantity",
          "quantity=0 rejected")
    check(_err_code(S.t_plan_order("005930", "BUY", "MARKET", quantity=-5)) == "invalid-quantity",
          "quantity<0 rejected")
    check(_err_code(S.t_plan_order("005930", "BUY", "LIMIT", quantity=1, price=0)) == "invalid-price",
          "price=0 rejected")
    check(_err_code(S.t_plan_order("NVDA", "BUY", "MARKET", quantity=1e-7)) == "invalid-quantity",
          "US qty rounding to 0 rejected")


def _gate_setup():
    """Flip into live + neutralise market-open so drift/FX branches are reachable."""
    object.__setattr__(S.CFG, "live", True)
    S.safety.check_market_open = lambda c: None
    S.CLIENT.get_market_calendar = lambda region, date=None: {}


def _snap(**kw):
    base = {"symbol": "005930", "side": "BUY", "currency": "KRW",
            "estimatedAmount": 70000, "snapshotPrice": 70000.0, "highValue": False,
            "account_index": 0, "timeInForce": "DAY"}
    base.update(kw)
    return base


def test_gate_currency_failclosed():
    print("\n[gate] currency fail-closed")
    _gate_setup()
    S.CLIENT.get_price = lambda s: {"lastPrice": "70000"}
    bad = _snap(orderType="LIMIT", price=70000, quantity=1, currency="", clientOrderId="CC0")
    check(_err_code(S._order_safety_gate(bad, "x")) == "INVALID_PARAM",
          "missing currency -> INVALID_PARAM (not skipped)")


def test_gate_estimated_fx():
    print("\n[gate] estimated-FX refusal (live USD)")
    _gate_setup()
    S.CLIENT.get_price = lambda s: {"lastPrice": "185.0"}
    usd = {"symbol": "NVDA", "side": "BUY", "orderType": "MARKET", "orderAmount": 100,
           "quantity": None, "price": None, "timeInForce": "DAY", "currency": "USD",
           "estimatedAmount": 100, "snapshotPrice": 185.0, "highValue": False,
           "account_index": 0, "clientOrderId": "FXA"}
    S.CLIENT.get_exchange_rate = lambda b, q, **k: {"rate": "0"}     # FX down
    check(_err_code(S._order_safety_gate(dict(usd), "x")) == "CURRENCY_MISMATCH",
          "no live FX rate -> refuse")
    S.CLIENT.get_exchange_rate = lambda b, q, **k: {"rate": "1350"}  # FX up
    check(S._order_safety_gate(dict(usd, clientOrderId="FXB"), "x") is None,
          "real FX rate -> proceed")


def test_gate_price_drift():
    print("\n[gate] price drift — MARKET/amount only, fail-closed when unverifiable")
    _gate_setup()
    # LIMIT with explicit price is bounded -> NOT drift-blocked even on +14%.
    S.CLIENT.get_price = lambda s: {"lastPrice": "80000"}
    check(S._order_safety_gate(_snap(orderType="LIMIT", price=70000, quantity=1,
                                     clientOrderId="DL1"), "x") is None,
          "LIMIT not over-blocked on market move")
    # MARKET fill tracks market -> +14% drift blocks.
    check(_err_code(S._order_safety_gate(_snap(orderType="MARKET", price=None, quantity=1,
                                               clientOrderId="DM1"), "x")) == "PRICE_MOVED",
          "MARKET +14% -> PRICE_MOVED")
    # within tolerance proceeds.
    S.CLIENT.get_price = lambda s: {"lastPrice": "70200"}
    check(S._order_safety_gate(_snap(orderType="MARKET", price=None, quantity=1,
                                     clientOrderId="DM2"), "x") is None,
          "MARKET +0.3% -> proceed")
    # price unverifiable -> fail CLOSED.
    S.CLIENT.get_price = lambda s: {}
    check(_err_code(S._order_safety_gate(_snap(orderType="MARKET", price=None, quantity=1,
                                               clientOrderId="DM3"), "x")) == "PRICE_MOVED",
          "MARKET price unverifiable -> fail closed")


def test_body_whitelist():
    print("\n[body] order POST whitelist + numeric->string")
    from toss_client import TossClient
    snap = {"symbol": "005930", "side": "BUY", "orderType": "LIMIT", "quantity": 3,
            "price": 70000, "timeInForce": "DAY", "currency": "KRW",
            "estimatedAmount": 210000, "snapshotPrice": 70000.0, "account_index": 0,
            "warnings": [], "clientOrderId": "B1", "confirmHighValueOrder": True}
    body = TossClient._order_body(snap, TossClient._ORDER_BODY_KEYS)
    check(set(body) <= set(TossClient._ORDER_BODY_KEYS), "only whitelisted keys")
    check("currency" not in body and "estimatedAmount" not in body
          and "snapshotPrice" not in body, "internal/junk fields not posted")
    check(body["price"] == "70000" and body["quantity"] == "3", "numbers -> strings")


def test_single_cancel_and_logout():
    print("\n[cancel/logout] single plan_cancel reachability + dedup + logout")
    open_orders = S.CLIENT.list_orders("OPEN").get("orders", [])
    check(bool(open_orders), "mock has an OPEN order to cancel")
    oid = open_orders[0].get("orderId")
    pc = S.t_plan_cancel(oid)
    check(bool(pc.get("preview_token")), "plan_cancel issues a preview_token (M-1 reachable)")
    check(pc.get("confirm_phrase", "").startswith("CANCEL "), "cancel confirm_phrase shaped")
    r1 = S.t_cancel_order_confirmed(pc["preview_token"], pc["confirm_phrase"])
    check(_err_code(r1) is None and r1.get("orderId"), "cancel#1 executes")
    # dedup: re-plan + confirm the SAME order_id -> DUPLICATE (M-2).
    pc2 = S.t_plan_cancel(oid)
    if pc2.get("preview_token"):
        r2 = S.t_cancel_order_confirmed(pc2["preview_token"], pc2["confirm_phrase"])
        check(_err_code(r2) == "DUPLICATE", "cancel#2 same order -> DUPLICATE")
    else:
        check(_err_code(pc2) == "INVALID_PARAM", "re-plan blocked (already terminal)")
    # bad id + wrong confirm.
    check(_err_code(S.t_plan_cancel("NOPE-9999")) == "order-not-found",
          "plan_cancel unknown order -> order-not-found")
    pc3 = S.t_plan_cancel(open_orders[-1].get("orderId"))
    if pc3.get("preview_token"):
        check(_err_code(S.t_cancel_order_confirmed(pc3["preview_token"], "wrong")) == "confirm-mismatch",
              "wrong confirm_phrase rejected on cancel")
    # logout (mock): clears token bookkeeping, no file.
    lo = S.t_logout()
    check(lo.get("tokenCleared") is True, "logout clears token")


def test_reserve_toctou():
    print("\n[TOCTOU] reserve-then-post: 동시 중복주문 원자 봉쇄")
    _reset_guard()
    check(S.GUARD.reserve_submission("RSV1") is None, "1차 예약 성공")
    dup = S.GUARD.reserve_submission("RSV1")
    check(dup is not None and dup["error"]["code"] == "DUPLICATE",
          "동시 2차 예약 -> DUPLICATE (레이스 봉쇄)")
    S.GUARD.release_submission("RSV1")            # definite failure path
    check(S.GUARD.reserve_submission("RSV1") is None, "확정실패 release 후 재시도 가능")
    S.GUARD.confirm_submission("RSV1", "OID1")    # success path
    S.GUARD.release_submission("RSV1")            # must NOT free a confirmed order
    dup2 = S.GUARD.check_duplicate("RSV1")
    check(dup2 is not None and dup2["error"]["code"] == "DUPLICATE",
          "confirm 후 release 무시(성공 주문 dedup 보존)")


def test_prereserve_failure_no_release():
    print("\n[H-NEW-1] pre-reserve 실패가 타 호출의 in-flight 예약을 해제하지 않는다")
    _reset_guard()
    # 동일 intent의 preview 2건은 같은 clientOrderId(시간독립 해시)를 공유한다.
    snap = {"symbol": "005930", "side": "BUY", "orderType": "LIMIT",
            "quantity": 2, "orderAmount": None, "price": 70000,
            "timeInForce": "DAY", "currency": "KRW", "estimatedAmount": 140000,
            "snapshotPrice": 70000.0, "highValue": True, "account_index": 0}
    tok = S.PREVIEWS.put(dict(snap))
    full = S.PREVIEWS.peek(tok)
    coid = full["clientOrderId"]
    # call-1이 이 coid를 예약 보유 중(in-flight)이라고 가정
    check(S.GUARD.reserve_submission(coid) is None, "call-1 예약 선점")
    # call-2: 같은 intent, confirm_high_value 누락 -> pre-reserve 예외 경로
    r = S.t_place_order_confirmed(tok, full["confirm_phrase"], confirm_high_value=False)
    check(_err_code(r) == "confirm-high-value-required", "pre-reserve 실패 발생")
    # call-1의 예약은 살아 있어야 한다 (해제됐다면 TOCTOU 창 재개방)
    dup = S.GUARD.reserve_submission(coid)
    check(dup is not None and dup["error"]["code"] == "DUPLICATE",
          "in-flight 예약 보존 (release 누수 없음)")


def _reset_guard():
    """Guard dedup/daily/circuit state is file-backed (crash-safe) and persists
    across runs — reset it so idempotency assertions are deterministic."""
    for p in (S.GUARD._dedup_path, S.GUARD._daily_path, S.GUARD._circuit_path):
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    _reset_guard()
    test_positivity()
    test_gate_currency_failclosed()
    test_gate_estimated_fx()
    test_gate_price_drift()
    test_body_whitelist()
    test_reserve_toctou()
    test_prereserve_failure_no_release()
    test_single_cancel_and_logout()
    print(f"\n=== order-safety: {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)
