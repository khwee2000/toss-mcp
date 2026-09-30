"""live_trade.py — REAL-money executor (전략 신호 → 실제 토스 주문).

⚠️ 이 스크립트만이 하네스에서 실주문을 낸다. 다중 게이트:
  server 4중 게이트(live+preview+confirm+kill) + 13대 불변식 위에,
  실행기 자체 게이트: LIVE_GO 마커(안전 재검수 사인오프, #79) · 정규장 한정 ·
  대형주문 2차 차단(#71) · 주문 버스트 auto-HALT(#73) · 물타기 금지 ·
  일일/주간 한도 · 수익목표 중단(#20) · 손실 후 비중 축소(#70) · 쿨다운 ·
  FOMO · 상관(#63) · 서킷 · 스프레드/신선도/최소금액(#11-13) · 비용효율 경고(#69).

체결 처리: 주문 폴링(#59) · 타임아웃(#16) · 부분체결(#15) · 실체결가 기록(#48/#32)
· 센트/수량 반올림(#30) · 진입 환율 기록(#27).

CLI:
  --enter SYM USD    실매수 (예: --enter AMD 45)
  --exit SYM SIG     실매도 (SIG: TP1_HALF/STOP/TRAIL_STOP/TREND_EXIT/TP2_REST/TIME_EXIT/CLOSE)
  --panic-exit       🚨 전략 실포지션 전량 시장가 청산(#77) (legacy 실계좌 보유는 절대 건드리지 않음)
  --status           실포지션·미체결 조회/reconcile
  --help             사용법
"""
from __future__ import annotations
import json, sys, time
from datetime import datetime, timedelta
from pathlib import Path

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S            # noqa: E402
import market_cycle as MC     # noqa: E402
import strategy_100k as ST    # noqa: E402
import risk_manager as RM     # noqa: E402

KST = MC.KST
GO_MARKER = Path.home() / ".toss-trader" / "LIVE_GO"      # #79 사인오프 게이트
HALT_FILE = Path.home() / ".toss-mcp" / "HALT"
REAL_CAPITAL_KRW = 120_000     # 실전 슬리브(입금액) — weight 분모
MAX_SINGLE_USD = 60.0          # #71: 이 초과는 사용자 직접 주문만
MAX_REAL_POSITIONS = 2         # 소액계좌: 4분할은 수수료가 먹음 → 최대 2
MAX_POS_FRAC = 0.60            # 슬리브 대비 한 종목 최대 60%
BURST_WINDOW_MIN = 10          # #73: 10분 내
BURST_MAX_ORDERS = 4           #      실주문 4건 초과 → HALT 자동 생성
POLL_TRIES, POLL_WAIT = 10, 3  # #59/#16: 체결 폴링 30초
DAILY_PROFIT_TARGET_PT = 3.0   # #20: 당일 +3pt 달성 시 신규 중단
CLOSE_SIGS = MC.CLOSE_SIGS


def _now():
    return datetime.now(KST)


def _real_positions(st):
    return [p for p in st["positions"] if p.get("real")]


def _recent_real_orders(minutes: int) -> int:
    """#73: 최근 N분간 실주문(진입/청산) 건수 (저널 기준)."""
    cutoff = (_now() - timedelta(minutes=minutes)).isoformat()
    n = 0
    try:
        for line in MC.JOURNAL.read_text().splitlines()[-200:]:
            d = json.loads(line)
            if d.get("event") in ("REAL_OPEN", "REAL_TRIM", "REAL_CLOSE") \
                    and d.get("ts", "") >= cutoff:
                n += 1
    except Exception:
        pass
    return n


def _burst_guard() -> bool:
    if _recent_real_orders(BURST_WINDOW_MIN) >= BURST_MAX_ORDERS:
        try:
            HALT_FILE.parent.mkdir(parents=True, exist_ok=True)
            HALT_FILE.touch()
        except Exception:
            pass
        print(f"🛑 이상거래 감지: {BURST_WINDOW_MIN}분 내 실주문 {BURST_MAX_ORDERS}건 초과 "
              f"→ HALT 자동 생성({HALT_FILE}). 수동 확인 후 파일 삭제로 해제.")
        MC.journal({"event": "ALERT", "kind": "auto_halt_burst"})
        return False
    return True


# Official OrderStatus enum (openapi.json). Classify against it — the old code
# only knew FILLED/REJECTED/CANCELED and mis-read PARTIAL_FILLED as a timeout.
# Official OrderStatus enum (openapi.json). TERMINAL = the order is done and
# will NOT fill further; anything else may still fill and must NOT be booked.
_TERMINAL = {"FILLED", "CANCELED", "REJECTED", "REPLACED"}


def _poll_fill(order_id: str):
    """Poll a live order to a TERMINAL state (or timeout). Returns
    (status, filled_qty, avg_price, outcome, cost_usd) where outcome ∈:
      'filled'           — FILLED, fully executed
      'partial_terminal' — TERMINAL (canceled/rejected/replaced) but some shares
                           really executed (fq>0) → those shares ARE held
      'dead'             — TERMINAL with ZERO fills → nothing happened
      'working'          — NON-terminal at timeout → outcome UNKNOWN, never book
    cost_usd = commission + tax from the execution (for honest P/L).
    Only 'filled'/'partial_terminal' are safe to book; 'dead'/'working' must
    leave the ledger unchanged (working = the order is still live)."""
    stt, fq, ap, cost = None, 0.0, None, 0.0
    for _ in range(POLL_TRIES):
        o = S.t_track_order(order_id=order_id)
        stt = (o or {}).get("status")
        ex = (o or {}).get("execution") or {}
        try:
            fq = float(ex.get("filledQuantity") or 0)
        except (TypeError, ValueError):
            fq = 0.0
        try:
            ap = float(ex.get("averageFilledPrice") or 0) or None
        except (TypeError, ValueError):
            ap = None
        try:
            cost = float(ex.get("commission") or 0) + float(ex.get("tax") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        if stt in _TERMINAL:                       # done — classify and stop
            if stt == "FILLED" and fq > 0 and ap:
                return stt, fq, ap, "filled", cost
            return stt, fq, ap, ("partial_terminal" if fq > 0 and ap else "dead"), cost
        time.sleep(POLL_WAIT)                       # PENDING/PARTIAL_FILLED -> keep polling
    return stt, fq, ap, "working", cost             # still live at timeout — do NOT book


_BOOKABLE = {"filled", "partial_terminal"}          # only these are safe to record


def _locked(timeout: float = 90.0):
    """Interprocess lock SHARED with market_cycle (~/.toss-trader/cycle.lock) so
    a live order's load→mutate→save can't interleave with a cron cycle's save
    (the lost-update race). Blocking with timeout; returns a ctx-manager."""
    class _L:
        def __enter__(self):
            self.lf = MC._acquire_lock(block=True, timeout=timeout)
            return self.lf
        def __exit__(self, *a):
            try:
                self.lf and self.lf.close()
            except Exception:
                pass
    return _L()


def preflight(st, symbol: str, usd_amt: float):
    """실매수 전 전수 가드. (ok, adjusted_usd, warnings) 반환."""
    warns = []
    if not S.CFG.allow_live_orders:
        return False, "실주문 모드 아님(ALLOW_LIVE_ORDERS=0)", warns
    if not S.CFG.live:
        warns.append("mock 모드 — 주문은 시뮬레이션 (실네트워크 차단)")
    if not GO_MARKER.exists():                                    # #79
        return False, f"LIVE_GO 마커 없음({GO_MARKER}) — 안전 재검수 GO 후 생성됨", warns
    if usd_amt > MAX_SINGLE_USD:                                  # #71
        return False, f"대형주문 2차 가드: ${usd_amt} > ${MAX_SINGLE_USD} — 사용자 직접 주문만", warns
    if MC.us_session() != "미국 정규장":
        return False, f"세션 {MC.us_session()} — 실주문은 정규장에만", warns
    # 오프닝/클로징 레인지 가드 (KST, 서머타임 정규장 22:30~05:00):
    #  · 개장 30분(22:30~23:00) 스파이크 구간 신규진입 금지
    #  · 마감 40분(04:20~05:00) 신규진입 금지 — 이 뒤엔 EOD 전량청산이 뜨므로
    #    새로 사봐야 바로 팔린다. 오버나잇 보유 자체를 안 만든다(갭 리스크 제거).
    from datetime import time as _dt
    t = _now().time()
    if _dt(22, 30) <= t < _dt(23, 0):
        return False, "개장 직후 30분(오프닝 레인지) — 관찰 전용, 신규진입 금지", warns
    if _dt(4, 20) <= t < _dt(5, 0):
        return False, "마감 40분(클로징 레인지) — 신규진입 금지(EOD 청산 임박)", warns
    if any(p["symbol"] == symbol for p in st["positions"]):
        return False, "이미 보유 — 물타기/추가매수 금지", warns
    if len(_real_positions(st)) >= MAX_REAL_POSITIONS:
        return False, f"실포지션 {MAX_REAL_POSITIONS}개 상한", warns
    dsx = st["dailyStats"]
    if dsx["entries"] >= MC.MAX_ENTRIES_PER_DAY:
        return False, "일일 진입 한도 소진", warns
    if dsx["lossCloses"] >= MC.MAX_LOSS_CLOSES_PER_DAY:
        return False, "당일 손실청산 한도 — 오늘 신규 금지", warns
    if st["weekStats"]["entries"] >= MC.MAX_ENTRIES_PER_WEEK:
        return False, "주간 진입 한도 소진", warns
    today = _now().strftime("%Y-%m-%d")
    until = st["cooldowns"].get(symbol)
    if until and today < until:
        return False, f"{symbol} 손절 쿨다운(~{until})", warns
    if st.get("reduceUntil") == today:                            # #70
        cap = round(usd_amt / 2, 2)
        warns.append(f"전일 손실중단 → 오늘 비중 반감: ${usd_amt}→${cap}")
        usd_amt = cap
    equity, daily, dd, ok_c, why_c = MC.equity_and_circuit(
        st, MC._open_contrib(st), today)
    if not ok_c:
        return False, f"서킷: {why_c}", warns
    if daily >= DAILY_PROFIT_TARGET_PT:                           # #20
        return False, f"수익목표 달성(오늘 {daily:+.1f}pt) — 신규 중단, 이익 보전", warns
    s = MC.snap(symbol)
    chg = (s or {}).get("changePct")
    if chg is not None and chg >= MC.FOMO_DAY_CHANGE_PCT:
        return False, f"FOMO: 당일 {chg:+.1f}% 추격 금지", warns
    if st["positions"]:                                            # #63
        ra = MC._returns20(symbol)
        for p in st["positions"]:
            c_ = MC._corr(ra, MC._returns20(p["symbol"])) if ra else None
            if c_ is not None and c_ > MC.MAX_CORR:
                return False, f"상관 {c_:.2f}>{MC.MAX_CORR} ({p['symbol']}) — 분산 아님", warns
    fx = 1495.0
    try:
        fx = float(S.CLIENT.get_exchange_rate("USD", "KRW")["rate"])
    except Exception:
        pass
    krw = usd_amt * fx
    frac = krw / REAL_CAPITAL_KRW
    if frac > MAX_POS_FRAC:
        return False, f"비중 {frac:.0%} > 상한 {MAX_POS_FRAC:.0%}", warns
    px, age = MC.price_meta(symbol)
    sp = MC.spread_pct(symbol)
    ok2, why2 = RM.check_order_data(age, sp, krw)                 # #11-13
    if not ok2:
        return False, f"주문데이터: {why2}", warns
    rt_cost = (sp or 0) + 0.2                                      # #69 왕복비용(스프레드+수수료 0.1×2)
    if rt_cost > ST.TP_LADDER[0][0] / 3:
        warns.append(f"⚠️ 비용효율: 왕복비용 {rt_cost:.2f}% (1차익절 {ST.TP_LADDER[0][0]}%의 1/3↑)")
    if not _burst_guard():                                         # #73
        return False, "auto-HALT 발동", warns
    return True, round(usd_amt, 2), warns


def cmd_enter(symbol: str, usd_amt: float) -> int:
    with _locked() as lk:
        if lk is None:
            print("⛔ cycle.lock 획득 실패(사이클/주문 실행 중) — 잠시 후 재시도")
            return 1
        st = MC.load_state()
        ok, res, warns = preflight(st, symbol, round(float(usd_amt), 2))   # #30 센트 반올림
        for w in warns:
            print("  ", w)
        if not ok:
            print("⛔ 실매수 거부:", res)
            return 1
        usd = res
        plan = S.t_plan_order(symbol, "BUY", "MARKET", order_amount=usd)
        if plan.get("error") or not plan.get("preview_token"):
            print("⛔ plan 거부:", json.dumps(plan.get("error"), ensure_ascii=False)[:160])
            return 1
        print(f"▶ 실매수 {symbol} ${usd} — confirm: {plan['confirm_phrase']!r}")
        r = S.t_place_order_confirmed(plan["preview_token"], plan["confirm_phrase"])
        if r.get("error") or not r.get("orderId"):
            print("⛔ 주문 거부:", json.dumps(r.get("error"), ensure_ascii=False)[:160])
            return 1
        oid = r.get("orderId")
        stt, fq, ap, outcome, cost = _poll_fill(oid)
        fx = 1495.0
        try:
            fx = float(S.CLIENT.get_exchange_rate("USD", "KRW")["rate"])   # #27
        except Exception:
            pass
        MC.journal({"event": "REAL_OPEN", "symbol": symbol, "orderId": oid,
                    "usd": usd, "fx": fx, "status": stt, "outcome": outcome,
                    "filledQty": fq, "avgFill": ap, "costUsd": cost})
        st = MC.load_state()                       # re-read under lock (fresh)
        # DEAD (거부/취소·0체결): 유령 포지션 절대 기록 금지 — 장부·현금·카운터 불변.
        if outcome == "dead":
            MC.save_state(st)                      # journal only; nothing booked
            print(f"⛔ {symbol} 주문 {stt} — 체결 0주, 포지션 미기록(장부 불변)")
            return 1
        # 실제 체결분만 장부화. actual notional = 체결수량×체결가×환율 (의도액 아님).
        confirmed = fq > 0 and ap                  # 실제 보유 주식이 있나
        bookable = outcome in _BOOKABLE            # 종결(더 안 채워짐)
        qty = round(fq, 6) if fq else 0.0
        weight = round(fq * ap * fx / REAL_CAPITAL_KRW, 4) if confirmed else 0.0
        entry = ap if confirmed else None
        pending = not (bookable and confirmed)     # working 이거나 미체결 → reconcile 대기
        pos = {"symbol": symbol, "entryRef": entry, "weight": weight,
               "entryDate": _now().strftime("%Y-%m-%d"),
               "highwater": entry, "tp1_done": False, "real": True,
               "orderId": oid, "qty": qty, "fx": fx, "entryCostUsd": round(cost, 4),
               "pendingFill": pending}
        st["positions"].append(pos)
        st["cashFrac"] = round(st["cashFrac"] - weight, 4)   # debit ACTUAL fill (0 if unconfirmed)
        st["dailyStats"]["entries"] += 1
        st["weekStats"]["entries"] += 1
        MC.save_state(st)
        if not pending:
            print(f"✅ 체결 {symbol} {fq}주 @ ${ap} (환율 {fx}, 비중 {weight}, 비용 ${cost})")
        else:
            print(f"⏳ {symbol} {stt} — 체결 {fq}주(확정보유 {bool(confirmed)}) → `--status` reconcile 필요")
            MC.journal({"event": "ALERT", "kind": "pending_fill",
                        "symbol": symbol, "orderId": oid})
        return 0


def cmd_exit(symbol: str, sig: str) -> int:
    with _locked() as lk:
        if lk is None:
            print("⛔ cycle.lock 획득 실패(사이클/주문 실행 중) — 잠시 후 재시도")
            return 1
        return _do_exit(symbol, sig)


def _do_exit(symbol: str, sig: str) -> int:
    """실매도 실행 (호출자가 cycle.lock 보유 가정). 매도가 실제로 체결된
    수량만 실현·차감하고, 거부/미체결이면 포지션을 그대로 둔다(고아화 방지)."""
    if sig not in CLOSE_SIGS and sig != "TP1_HALF":
        print("⛔ 시그널:", sorted(CLOSE_SIGS | {"TP1_HALF"}))
        return 1
    st = MC.load_state()
    pos = next((p for p in st["positions"] if p["symbol"] == symbol and p.get("real")), None)
    if not pos:
        print("⛔ 실포지션 아님:", symbol)
        return 1
    if pos.get("pendingFill") or not pos.get("qty") or not pos.get("entryRef"):
        print("⛔ 체결 미확정 — 먼저 `--status`로 reconcile")
        return 1
    if MC.us_session() == "미국장 휴장":
        print("⛔ 휴장 — 매도 불가 시간")
        return 1
    qty = float(pos["qty"])
    half = sig == "TP1_HALF"
    sell_qty = round(qty / 2, 6) if half else qty
    est_usd = sell_qty * float(pos["entryRef"])
    if half and est_usd * float(pos.get("fx") or 1495) < RM.RISK["min_order_krw"]:
        print("절반이 최소주문 미만 → 전량 청산으로 전환")
        half, sell_qty, sig = False, qty, "TP2_REST"
    plan = S.t_plan_order(symbol, "SELL", "MARKET", quantity=sell_qty)
    if plan.get("error") or not plan.get("preview_token"):
        print("⛔ plan 거부:", json.dumps(plan.get("error"), ensure_ascii=False)[:160])
        return 1
    print(f"▶ 실매도 {symbol} {sell_qty}주 [{sig}] — confirm: {plan['confirm_phrase']!r}")
    r = S.t_place_order_confirmed(plan["preview_token"], plan["confirm_phrase"])
    if r.get("error") or not r.get("orderId"):
        print("⛔ 주문 거부:", json.dumps(r.get("error"), ensure_ascii=False)[:160])
        return 1
    oid = r.get("orderId")
    stt, fq, ap, outcome, cost = _poll_fill(oid)
    # 종결(더 안 채워짐) + 실제 체결분이 있을 때만 장부 변이. working(주문 아직
    # 살아있음)·dead(0체결)·가격불명은 전부 상태 불변 — 고아화·이중계상 방지.
    if outcome not in _BOOKABLE or fq <= 0 or not ap:
        MC.journal({"event": "REAL_SELL_FAIL", "symbol": symbol, "orderId": oid,
                    "signal": sig, "status": stt, "outcome": outcome, "filledQty": fq})
        note = "주문 아직 진행중 — reconcile 필요" if outcome == "working" else "미체결/거부"
        print(f"⛔ {symbol} 매도 미확정({stt}, {note}) — 포지션 유지, 재시도")
        return 1
    st = MC.load_state()                             # fresh under lock
    pos2 = next((p for p in st["positions"] if p["symbol"] == symbol and p.get("real")), None)
    if not pos2:                                     # vanished (shouldn't under lock)
        print("⚠️ 포지션 소실 — 저널만 기록")
        MC.journal({"event": "REAL_SELL_ORPHAN", "symbol": symbol, "orderId": oid,
                    "filledQty": fq, "avgFill": ap})
        return 1
    w = float(pos2["weight"])
    sold = min(fq, qty)                              # 실제 매도 체결 수량
    frac = sold / qty if qty else 0.0
    # #N4 수수료·세금 반영: 실현수익률에서 왕복비용을 % 드래그로 차감(낙관편향 제거).
    entry_cost = float(pos2.get("entryCostUsd") or 0) * (sold / qty if qty else 0)
    cost_pct = ((entry_cost + cost) / (sold * pos2["entryRef"]) * 100) if (sold and pos2["entryRef"]) else 0.0
    plp = (ap / pos2["entryRef"] - 1) * 100
    net_plp = plp - cost_pct
    gain = round(w * frac * net_plp, 3)
    st["realizedPlPct"] = round(st.get("realizedPlPct", 0.0) + gain, 3)
    st["cashFrac"] = round(st["cashFrac"] + w * frac, 4)
    remaining = round(qty - sold, 6)
    today = _now().strftime("%Y-%m-%d")
    # 청산 여부는 '의도'가 아니라 '실제 잔량'으로 결정 — 전량청산 의도라도 부분만
    # 체결되면 잔여 실주식이 남으므로 포지션을 유지해야 한다(고아화 방지).
    full_close = remaining <= 1e-9
    if not full_close:
        pos2["qty"] = remaining
        pos2["weight"] = round(w * (1 - frac), 4)
        pos2["entryCostUsd"] = round(float(pos2.get("entryCostUsd") or 0) * (1 - (sold / qty if qty else 0)), 4)
        # #N6 tp1_done은 '의도한 절반이 실제로 팔렸을 때만' — 소량 부분체결이 남은
        # 오버사이즈 잔량의 추가 TP1을 막지 않도록.
        if half and sold >= sell_qty * 0.9:
            pos2["tp1_done"] = True
        MC.journal({"event": "REAL_TRIM", "symbol": symbol, "orderId": oid, "signal": sig,
                    "soldQty": sold, "plPct": round(plp, 2), "netPlPct": round(net_plp, 2),
                    "realized": gain, "costUsd": cost, "avgFill": ap, "status": stt})
        print(f"✂️ {symbol} 부분매도 {sold}주 [{sig}] {net_plp:+.1f}%순 (잔량 {remaining})")
    else:
        st["positions"] = [p for p in st["positions"]
                           if not (p["symbol"] == symbol and p.get("real"))]
        if sig in ("STOP", "TRAIL_STOP") or plp < 0:
            st["cooldowns"][symbol] = (_now() + timedelta(days=MC.COOLDOWN_DAYS)).strftime("%Y-%m-%d")
        if plp < 0:
            st["dailyStats"]["lossCloses"] += 1
        if sig == "TP1_HALF":       # 잔량이 사실상 0으로 전량 소진된 TP1도 완료 표시
            pass
        MC.journal({"event": "REAL_CLOSE", "symbol": symbol, "orderId": oid, "signal": sig,
                    "soldQty": sold, "plPct": round(plp, 2), "netPlPct": round(net_plp, 2),
                    "realized": gain, "costUsd": cost, "avgFill": ap, "status": stt})
        print(f"✅ {symbol} 청산 {sold}주 [{sig}] {net_plp:+.1f}%순 @ ${ap} ({stt})")
    MC.save_state(st)
    return 0


def cmd_panic() -> int:
    """#77 비상 전량청산 — 전략 실포지션만. legacy 실계좌 보유는 절대 미접촉.
    락을 1회 잡고 _do_exit를 재사용(중첩 락 방지)."""
    with _locked() as lk:
        if lk is None:
            print("⛔ cycle.lock 획득 실패 — 잠시 후 재시도")
            return 1
        reals = _real_positions(MC.load_state())
        if not reals:
            print("실포지션 없음")
            return 0
        print(f"🚨 PANIC EXIT — 전략 실포지션 {len(reals)}종목 전량 시장가 청산")
        rc = 0
        for p in list(reals):
            rc |= _do_exit(p["symbol"], "CLOSE")
        return rc


def cmd_status() -> int:
    with _locked() as lk:
        if lk is None:
            print("⛔ cycle.lock 획득 실패 — 잠시 후 재시도")
            return 1
        st = MC.load_state()
        reals = _real_positions(st)
        print(f"실포지션 {len(reals)}개 | 현금 {st['cashFrac']:.2f} | "
              f"실현 {st.get('realizedPlPct',0):+.2f}pt")
        dirty = False
        for p in list(reals):
            line = f"  {p['symbol']}: {p.get('qty')}주 @ ${p.get('entryRef')} w={p['weight']}"
            if p.get("pendingFill"):
                stt, fq, ap, outcome, cost = _poll_fill(p["orderId"])
                if outcome == "dead":                # 유령 매수 — 제거 + 차감액·카운터 환원
                    st["cashFrac"] = round(st["cashFrac"] + float(p.get("weight") or 0), 4)
                    st["positions"] = [q for q in st["positions"] if q is not p]
                    ds = st.get("dailyStats", {}); ws = st.get("weekStats", {})   # #N5 카운터 환불
                    ds["entries"] = max(0, int(ds.get("entries", 0)) - 1)
                    ws["entries"] = max(0, int(ws.get("entries", 0)) - 1)
                    dirty = True
                    line += f"  → 제거(체결 0, {stt}); 현금·진입카운터 환원"
                elif outcome in _BOOKABLE and fq > 0 and ap:   # 종결+실체결 → 확정(활성화)
                    fx = float(p.get("fx") or 1495)
                    new_w = round(fq * ap * fx / REAL_CAPITAL_KRW, 4)
                    st["cashFrac"] = round(st["cashFrac"] - (new_w - float(p.get("weight") or 0)), 4)
                    p["qty"], p["entryRef"], p["highwater"], p["weight"] = round(fq, 6), ap, ap, new_w
                    p["entryCostUsd"] = round(cost, 4)
                    p["pendingFill"] = False          # 종결 — 더 이상 안 채워짐, 관리 대상 활성화
                    dirty = True
                    line += f"  → 확정: 체결 {fq}주 @ ${ap} w={new_w} (관리개시)"
                else:                                 # working — 아직 살아있음, 계속 대기
                    line += f"  ⏳ 진행중({stt})"
            print(line)
        if dirty:
            MC.save_state(st)
        try:
            oo = S.t_get_orders("OPEN").get("orders", [])
            print(f"미체결 주문: {len(oo)}건")
        except Exception:
            pass
        return 0


USAGE = """live_trade.py — 실주문 실행기 (LIVE_GO 마커 필요)
  --enter SYM USD   실매수 (정규장만, ≤$60, 전 가드 통과 시)
  --exit SYM SIG    실매도 (TP1_HALF/STOP/TRAIL_STOP/TREND_EXIT/TP2_REST/TIME_EXIT/CLOSE)
  --panic-exit      전략 실포지션 전량 청산 (legacy 미접촉)
  --status          포지션/미체결 확인 + pendingFill reconcile
긴급정지: TOSS_KILL=1 또는 touch ~/.toss-mcp/HALT"""

if __name__ == "__main__":
    a = sys.argv
    if len(a) > 3 and a[1] == "--enter":
        sys.exit(cmd_enter(a[2], float(a[3])))
    elif len(a) > 3 and a[1] == "--exit":
        sys.exit(cmd_exit(a[2], a[3]))
    elif len(a) > 1 and a[1] == "--panic-exit":
        sys.exit(cmd_panic())
    elif len(a) > 1 and a[1] == "--status":
        sys.exit(cmd_status())
    else:
        print(USAGE)
