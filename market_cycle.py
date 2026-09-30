"""market_cycle.py — 30-minute market re-assessment cycle (v3).

v3 (2차 업그레이드 25종 중 하네스 분):
  판단  #1 RS·#2 ATR-적응 트레일·#3 regime 히스테리시스·#4 갭·#6 세션 스캔캐시
  행동  #13 재진입 쿨다운·#14 연속손실 중단·#15 일일진입 한도·#16 FOMO
        #17 서킷 시 진입차단·#18 비중범위·#19 세션 가드
  현실화 #25 슬리피지(호가스프레드 절반) 반영
  견고성 #33 원자저장·#34 백업/undo·#35 저널 로테이션·#36 헬스체크
        #37 데이터실패 카운터·#38 스키마 v3
  리포트 #43 --report·#44 보유일 저널·#45 regime 플립 저널

CLI:
  (none)              사이클 리포트
  --apply '<json>'    상태 교체(검증+마이그레이션, 자동 .bak)
  --enter SYM W       페이퍼 진입 (12중 가드: 비중/쿨다운/일일한도/연속손실/
                      서킷/세션/FOMO/리스크/주문데이터)
  --signal SYM SIG    TP1_HALF/STOP/TRAIL_STOP/TREND_EXIT/TP2_REST/TIME_EXIT/CLOSE
  --report            성과 리포트 (승률·PF·기대값)
  --undo              직전 상태 변경 취소(.bak 복원)

Paper state: ~/.toss-trader/paper_portfolio.json (v3) · Journal: journal.jsonl
READ-ONLY on the live account — never places a real order.
"""
from __future__ import annotations
import hashlib
import json, os, shutil, sys, time
from datetime import datetime, timezone, timedelta, time as dtime
from pathlib import Path
from typing import Dict, Optional

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S           # noqa: E402
import indicators as ind     # noqa: E402
import trading_rules as TR   # noqa: E402
import strategy_100k as ST   # noqa: E402
import risk_manager as RM    # noqa: E402
from config import load_config  # noqa: E402

KST = timezone(timedelta(hours=9))
STATE = Path(os.path.expanduser("~/.toss-trader/paper_portfolio.json"))
JOURNAL = Path(os.path.expanduser("~/.toss-trader/journal.jsonl"))
STATE_VERSION = 3
TRAIL_PCT = 8.0                      # base trail; #2 widens by ATR
CLOSE_SIGS = {"STOP", "TRAIL_STOP", "TREND_EXIT", "TP2_REST", "TIME_EXIT", "CLOSE"}
COOLDOWN_DAYS = 2                    # #13
MAX_ENTRIES_PER_DAY = 3              # #15
MAX_LOSS_CLOSES_PER_DAY = 2          # #14
FOMO_DAY_CHANGE_PCT = 8.0            # #16
WEIGHT_MIN, WEIGHT_MAX = 0.05, 0.25  # #18
JOURNAL_MAX_BYTES = 2_000_000        # #35
FEE_PCT_US = 0.1                     # 수수료 모델: 토스 US 편도 0.1% (live commissions 확인값)
MAX_CORR = 0.85                      # 상관 가드: 보유종목과 20일 수익률 상관 상한
MAX_ENTRIES_PER_WEEK = 8             # 주간 매매횟수 상한
ALERT_DROP_PCT = -10.0               # 급락 이상징후 (손절 -7%를 갭으로 뚫은 상황)
_DATA_FAILS = 0                      # #37


def _fee_pct(symbol: str) -> float:
    """US(비숫자 심볼)만 편도 수수료 적용; KR은 현재 매수/매도 수수료 면제."""
    return FEE_PCT_US if not str(symbol).isdigit() else 0.0


def _returns20(symbol):
    """20일 일별 수익률 시계열 (상관 가드용)."""
    c = _retry(lambda: S.CLIENT.get_candles(symbol, "1d", 30).get("candles", []))
    if not c or len(c) < 21:
        return None
    closes = [float(x["close"]) for x in c][-21:]
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes))]


def _corr(a, b):
    n = min(len(a or []), len(b or []))
    if n < 10:
        return None
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return None
    return cov / (va ** 0.5 * vb ** 0.5)


def _checksum(st: Dict) -> str:
    """상태 무결성 체크섬 (수동 편집/오염 감지; updated/checksum 제외)."""
    core = {k: v for k, v in st.items() if k not in ("_checksum", "updated", "_integrity")}
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- utilities
def _retry(fn, n=3, pause=1.2):
    global _DATA_FAILS
    for _ in range(n):
        try:
            r = fn()
            if r:
                return r
        except Exception:
            time.sleep(pause)
    _DATA_FAILS += 1
    return None


def snap(sym):
    """Indicators + atrPct(#2) + gapPct(#4) from one candles fetch."""
    def _f():
        candles = S.CLIENT.get_candles(sym, "1d", 160).get("candles", [])
        if not candles:
            return None
        r = ind.compute(candles)
        r["atrPct"] = TR.atr_pct(candles, 14)
        if len(candles) >= 2:
            try:
                r["gapPct"] = round((float(candles[-1]["open"]) /
                                     float(candles[-2]["close"]) - 1) * 100, 2)
            except Exception:
                pass
        return r
    return _retry(_f)


def price_meta(symbol):
    r = _retry(lambda: S.CLIENT.get_price(symbol))
    if not r:
        return None, None
    try:
        px = float(r["lastPrice"])
    except (TypeError, ValueError, KeyError):
        return None, None
    age = None
    try:
        ts = datetime.fromisoformat(str(r.get("timestamp")))
        age = abs((datetime.now(KST) - ts).total_seconds())
    except Exception:
        pass
    return px, age


def price(symbol):
    return price_meta(symbol)[0]


def spread_pct(symbol):
    ob = _retry(lambda: S.CLIENT.get_orderbook(symbol))
    try:
        b = float(ob["bids"][0]["price"]); a = float(ob["asks"][0]["price"])
        return round((a - b) / ((a + b) / 2) * 100, 2)
    except Exception:
        return None


def us_session(now: Optional[datetime] = None) -> str:
    """미국 시장 세션(ET 기준·EDT/EST 자동·주말 휴장). 겨울 EST에도 정확(CX4).
    조회 불가 시 KST 고정(EDT 근사)로 폴백."""
    base = now or datetime.now(KST)
    try:
        from zoneinfo import ZoneInfo
        if base.tzinfo is None:
            base = base.replace(tzinfo=KST)
        et = base.astimezone(ZoneInfo("America/New_York"))
        if et.weekday() >= 5:              # 토·일
            return "미국장 휴장"
        h = et.hour + et.minute / 60.0
        if 9.5 <= h < 16:
            return "미국 정규장"
        if 4 <= h < 9.5:
            return "미국 프리마켓"
        if 16 <= h < 20:
            return "미국 애프터마켓"
        return "미국장 휴장"
    except Exception:
        t = base.time()                    # 폴백: KST 고정(EDT 근사)
        if t >= dtime(22, 30) or t < dtime(5, 0):
            return "미국 정규장"
        if t >= dtime(17, 0):
            return "미국 프리마켓"
        if t < dtime(9, 0):
            return "미국 애프터마켓"
        return "미국장 휴장"


# ---------------------------------------------------------------- state (v3)
def load_state() -> Dict:
    try:
        st = json.loads(STATE.read_text())
    except Exception:
        st = {}
    # #39 integrity: verify checksum written by save_state (tamper detection)
    integ = "ok"
    if isinstance(st, dict) and st.get("_checksum"):
        if st["_checksum"] != _checksum(st):
            integ = "MISMATCH"
    st["_integrity"] = integ
    st.setdefault("positions", [])
    st.setdefault("cashFrac", 1.0)
    st.setdefault("realizedPlPct", 0.0)
    st.setdefault("peakEquityPct", 0.0)
    st.setdefault("dayStart", None)
    st.setdefault("lastRegime", None)
    st.setdefault("cooldowns", {})                       # #13 {sym: until-date}
    st.setdefault("regimeStreak", {"regime": None, "count": 0})  # #3
    st.setdefault("lastRunAt", None)                     # #36
    st.setdefault("lastScan", None)                      # #6
    today = datetime.now(KST).strftime("%Y-%m-%d")
    ds = st.get("dailyStats")                            # #14/#15 daily counters
    if not ds or ds.get("date") != today:
        st["dailyStats"] = {"date": today, "entries": 0, "lossCloses": 0}
    wk = datetime.now(KST).strftime("%G-W%V")            # 주간 매매횟수 상한
    ws = st.get("weekStats")
    if not ws or ws.get("week") != wk:
        st["weekStats"] = {"week": wk, "entries": 0}
    for p in st["positions"]:
        p.setdefault("entryDate", today)
        p.setdefault("highwater", p.get("entryRef"))
        p.setdefault("tp1_done", False)
    st["version"] = STATE_VERSION
    return st


def validate_state(st: Dict) -> list:
    errs = []
    if not isinstance(st.get("positions"), list):
        return ["positions는 list여야 함"]
    total = float(st.get("cashFrac", 0) or 0)
    for p in st["positions"]:
        for k in ("symbol", "entryRef", "weight"):
            if k not in p:
                errs.append(f"{p.get('symbol', '?')}: '{k}' 누락")
        total += float(p.get("weight", 0) or 0)
    if not (0.85 <= total <= 1.10):
        errs.append(f"비중합 {total:.2f} 비정상(0.85~1.10 밖)")
    return errs


def save_state(st: Dict) -> None:
    """#33 atomic write + #34 keep a .bak of the previous state for --undo."""
    st["updated"] = datetime.now(KST).isoformat(timespec="seconds")
    st.pop("_integrity", None)
    st["_checksum"] = _checksum(st)          # #39 integrity seal
    STATE.parent.mkdir(parents=True, exist_ok=True)
    if STATE.exists():
        try:
            shutil.copy2(STATE, STATE.with_suffix(".json.bak"))
        except Exception:
            pass
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2))
    os.replace(tmp, STATE)


def journal(event: Dict) -> None:
    """#31 append-only journal, #35 size-rotated."""
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        if JOURNAL.exists() and JOURNAL.stat().st_size > JOURNAL_MAX_BYTES:
            JOURNAL.rename(JOURNAL.with_name(
                f"journal-{datetime.now(KST).strftime('%Y%m%d%H%M%S')}.jsonl"))
        event = {"ts": datetime.now(KST).isoformat(timespec="seconds"), **event}
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ------------------------------------------------------------- core logic
def evaluate_position(p: Dict, cur=None, s=None, today=None) -> Dict:
    """Priority: STOP/TRAIL(#21, ATR-adaptive #2) > TREND_EXIT > TP1 > TP2 >
    TIME_EXIT(#25) > HOLD. Injectable for tests."""
    cur = cur if cur is not None else price(p["symbol"])
    s = s if s is not None else snap(p["symbol"])
    if cur is None or not s:
        return {"symbol": p["symbol"], "signal": "DATA?", "plPct": None,
                "reason": "조회실패", "highwater": p.get("highwater")}
    hw = max(float(p.get("highwater") or p["entryRef"]), cur)
    plp = (cur / p["entryRef"] - 1) * 100
    r = TR.RULESETS[ST.RULESET]
    try:
        d0 = datetime.strptime(p.get("entryDate", ""), "%Y-%m-%d").date()
        days = ((today or datetime.now(KST).date()) - d0).days
    except Exception:
        days = 0
    ma20 = s.get("ma20")
    atr = s.get("atrPct")
    trail = max(TRAIL_PCT, 1.5 * atr) if atr else TRAIL_PCT     # #2

    ts = TR.trailing_stop(p["entryRef"], hw, cur, trail, r["stop_pct"])
    if ts["triggered"]:
        sig = "TRAIL_STOP" if ts["locked"] else "STOP"
        why = f"{plp:+.1f}% — {ts['reason']}"
    elif ma20 and cur < ma20:
        sig, why = "TREND_EXIT", f"{plp:+.1f}% — 현재가<{round(ma20)} MA20 이탈"
    elif not p.get("tp1_done") and plp >= ST.TP_LADDER[0][0]:
        sig, why = "TP1_HALF", f"{plp:+.1f}% ≥ +{ST.TP_LADDER[0][0]:.0f}% → 절반 익절"
    elif plp >= ST.TP_LADDER[-1][0]:
        sig, why = "TP2_REST", f"{plp:+.1f}% ≥ +{ST.TP_LADDER[-1][0]:.0f}% → 잔량 익절"
    else:
        te = TR.time_exit(days, plp)
        if te["exit"]:
            sig, why = "TIME_EXIT", te["reason"]
        else:
            sig, why = "HOLD", f"{plp:+.1f}% (보유 {days}일, 손절선 {ts['stop']})"
    return {"symbol": p["symbol"], "signal": sig, "plPct": round(plp, 1), "cur": cur,
            "reason": why, "highwater": hw, "daysHeld": days, "stop": ts["stop"]}


def equity_and_circuit(st: Dict, open_contrib: float, today: str):
    equity = round(st.get("realizedPlPct", 0.0) + open_contrib, 2)
    ds = st.get("dayStart")
    if not ds or ds.get("date") != today:
        st["dayStart"] = {"date": today, "equity": equity}
    daily = round(equity - st["dayStart"]["equity"], 2)
    st["peakEquityPct"] = round(max(st.get("peakEquityPct", 0.0), equity), 2)
    dd = round(equity - st["peakEquityPct"], 2)
    ok, why = RM.check_circuit(daily, dd)
    return equity, daily, dd, ok, why


def effective_regime(st: Dict, raw: str) -> str:
    """#3 hysteresis: a regime change only takes effect after 2 consecutive
    cycles agree (first-ever reading applies immediately)."""
    stk = st.get("regimeStreak") or {"regime": None, "count": 0}
    if stk.get("regime") == raw:
        stk["count"] = int(stk.get("count", 0)) + 1
    else:
        stk = {"regime": raw, "count": 1}
    st["regimeStreak"] = stk
    last = st.get("lastRegime")
    return raw if (last is None or stk["count"] >= 2) else last


def _open_contrib(st: Dict) -> float:
    total = 0.0
    for p in st["positions"]:
        # 미체결(pendingFill)·entryRef 미확정 포지션은 손익 기여에서 제외 —
        # None/0으로 나눗셈 크래시 방지(live_trade.preflight도 이 함수를 호출).
        er = p.get("entryRef")
        if p.get("pendingFill") or not er:
            continue
        px = price(p["symbol"])
        if px is not None:
            total += p.get("weight", 0.0) * ((px / er - 1) * 100)
    return round(total, 3)


# ------------------------------------------------------------- subcommands
def cmd_enter(symbol: str, weight: float) -> int:
    st = load_state()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    # #18 weight bounds
    if not (WEIGHT_MIN <= weight <= WEIGHT_MAX):
        print(f"⛔ 비중 {weight} — 허용범위 {WEIGHT_MIN}~{WEIGHT_MAX}"); return 1
    # 물타기/추가매수 금지: 한 종목 한 번의 진입 원칙 (averaging-down ban)
    if any(p["symbol"] == symbol for p in st["positions"]):
        print(f"⛔ {symbol} 이미 보유 중 — 물타기/추가매수 금지(단일 진입 원칙)"); return 1
    # 주간 매매횟수 상한 (과매매 방지)
    if st["weekStats"]["entries"] >= MAX_ENTRIES_PER_WEEK:
        print(f"⛔ 주간 신규진입 한도 {MAX_ENTRIES_PER_WEEK}회 소진"); return 1
    # #13 re-entry cooldown
    until = st["cooldowns"].get(symbol)
    if until and today < until:
        print(f"⛔ {symbol} 손절 쿨다운 중 (~{until})"); return 1
    # #15 daily entry limit / #14 consecutive-loss halt
    dsx = st["dailyStats"]
    if dsx["entries"] >= MAX_ENTRIES_PER_DAY:
        print(f"⛔ 일일 신규진입 한도 {MAX_ENTRIES_PER_DAY}회 소진"); return 1
    if dsx["lossCloses"] >= MAX_LOSS_CLOSES_PER_DAY:
        print(f"⛔ 당일 손실청산 {dsx['lossCloses']}회 — 오늘 신규 금지(연속손실 가드)"); return 1
    # #19 session guard
    if us_session() == "미국장 휴장":
        print("⛔ 미국장 휴장 — 시세 스테일, 진입 보류"); return 1
    # #17 circuit check (was missing in v2)
    _, _, _, ok_c, why_c = equity_and_circuit(st, _open_contrib(st), today)
    if not ok_c:
        print("⛔ 서킷 발동 중 — 진입 금지:", why_c); return 1
    # #16 FOMO: don't chase a big up-day
    s = snap(symbol)
    chg = (s or {}).get("changePct")
    if chg is not None and chg >= FOMO_DAY_CHANGE_PCT:
        print(f"⛔ FOMO 가드: 당일 {chg:+.1f}% 급등 추격 금지"); return 1
    # 상관 가드: 보유종목과 20일 수익률 상관이 높으면 분산이 아님 (숨은 집중)
    if st["positions"]:
        ra = _returns20(symbol)
        for p in st["positions"]:
            c_ = _corr(ra, _returns20(p["symbol"])) if ra else None
            if c_ is not None and c_ > MAX_CORR:
                print(f"⛔ 상관 가드: {p['symbol']}와 상관 {c_:.2f} > {MAX_CORR} — "
                      "같은 방향으로 움직이는 종목(분산 효과 없음)"); return 1
    # risk manager (#1-8) + order-data (#11-13)
    dep = ST.DEPLOY.get(st.get("lastRegime") or "risk_off", 0.6)
    ok, why = RM.check_new_entry(st["positions"], st["cashFrac"], symbol, weight, dep)
    if not ok:
        print("⛔ 진입 거부(리스크):", why); return 1
    px, age = price_meta(symbol)
    if px is None:
        print("⛔ 시세 조회 실패"); return 1
    sp = spread_pct(symbol)
    ok2, why2 = RM.check_order_data(age, sp, ST.CAPITAL_KRW * weight)
    if not ok2:
        print("⛔ 진입 거부(주문데이터):", why2); return 1
    # #25 slippage(~ask) + 수수료: 페이퍼 체결가에 실제 비용 반영
    eff = round(px * (1 + (sp or 0) / 200.0) * (1 + _fee_pct(symbol) / 100.0), 4)
    st["positions"].append({"symbol": symbol, "entryRef": eff, "weight": weight,
                            "entryDate": today, "highwater": eff, "tp1_done": False})
    st["cashFrac"] = round(st["cashFrac"] - weight, 4)
    st["dailyStats"]["entries"] += 1
    st["weekStats"]["entries"] += 1
    journal({"event": "OPEN", "symbol": symbol, "entry": eff, "raw": px,
             "slipPct": round((sp or 0) / 2, 3), "feePct": _fee_pct(symbol),
             "weight": weight})
    save_state(st)
    print(f"OPEN {symbol} @{eff} (호가 {px}+슬리피지) w={weight} (현금 {st['cashFrac']:.2f})")
    return 0


def cmd_signal(symbol: str, sig: str, cur=None) -> int:
    st = load_state()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    pos = next((p for p in st["positions"] if p["symbol"] == symbol), None)
    if not pos:
        print("⛔ 보유 포지션 아님:", symbol); return 1
    cur = cur if cur is not None else price(symbol)
    if cur is None:
        print("⛔ 시세 조회 실패"); return 1
    sp = spread_pct(symbol) or 0.0
    # #25 sells at ~bid, minus one-way commission
    eff = cur * (1 - sp / 200.0) * (1 - _fee_pct(symbol) / 100.0)
    plp = (eff / pos["entryRef"] - 1) * 100
    w = float(pos["weight"])
    try:
        d0 = datetime.strptime(pos.get("entryDate", ""), "%Y-%m-%d").date()
        days = (datetime.now(KST).date() - d0).days    # #44
    except Exception:
        days = None
    if sig == "TP1_HALF":
        gain = round(w / 2 * plp, 3)
        st["realizedPlPct"] = round(st.get("realizedPlPct", 0.0) + gain, 3)
        pos["weight"] = round(w / 2, 4)
        pos["tp1_done"] = True
        st["cashFrac"] = round(st["cashFrac"] + w / 2, 4)
        journal({"event": "TRIM", "symbol": symbol, "plPct": round(plp, 2),
                 "realized": gain, "daysHeld": days})
        print(f"TRIM {symbol} 절반익절 {plp:+.1f}% (실현 {gain:+}pt)")
    elif sig in CLOSE_SIGS:
        gain = round(w * plp, 3)
        st["realizedPlPct"] = round(st.get("realizedPlPct", 0.0) + gain, 3)
        st["cashFrac"] = round(st["cashFrac"] + w, 4)
        st["positions"] = [p for p in st["positions"] if p["symbol"] != symbol]
        if sig in ("STOP", "TRAIL_STOP") or plp < 0:   # #13 arm cooldown
            until = (datetime.now(KST) + timedelta(days=COOLDOWN_DAYS)).strftime("%Y-%m-%d")
            st["cooldowns"][symbol] = until
        if plp < 0:                                    # #14 count loss closes
            st["dailyStats"]["lossCloses"] += 1
        journal({"event": "CLOSE", "symbol": symbol, "signal": sig,
                 "plPct": round(plp, 2), "realized": gain, "daysHeld": days})
        print(f"CLOSE {symbol} [{sig}] {plp:+.1f}% (실현 {gain:+}pt, {days}일 보유)")
    else:
        print("⛔ 알 수 없는 시그널:", sig); return 1
    save_state(st)
    return 0


def report_stats() -> Dict:
    """#43 — win rate / expectancy / profit factor from the journal."""
    closes, trims = [], 0
    files = sorted(JOURNAL.parent.glob("journal*.jsonl")) if JOURNAL.parent.exists() else []
    for fp in files:
        try:
            for line in fp.read_text().splitlines():
                d = json.loads(line)
                if d.get("event") == "CLOSE":
                    closes.append(d)
                elif d.get("event") == "TRIM":
                    trims += 1
        except Exception:
            continue
    n = len(closes)
    wins = [c.get("realized", 0.0) for c in closes if c.get("realized", 0.0) > 0]
    losses = [c.get("realized", 0.0) for c in closes if c.get("realized", 0.0) <= 0]
    total = round(sum(c.get("realized", 0.0) for c in closes), 3)
    pf = round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else None
    by_sig: Dict[str, Dict] = {}                 # 시그널별 성과 분해 (#49)
    for c in closes:
        d = by_sig.setdefault(c.get("signal", "?"), {"n": 0, "pl": 0.0})
        d["n"] += 1
        d["pl"] = round(d["pl"] + c.get("realized", 0.0), 3)
    return {"closes": n, "wins": len(wins), "losses": len(losses), "trims": trims,
            "bySignal": by_sig,
            "winRate": round(len(wins) / n * 100, 1) if n else None,
            "totalRealized": total,
            "expectancy": round(total / n, 3) if n else None,
            "avgWin": round(sum(wins) / len(wins), 3) if wins else None,
            "avgLoss": round(sum(losses) / len(losses), 3) if losses else None,
            "profitFactor": pf}


def cmd_report() -> int:
    r = report_stats()
    print("===== 성과 리포트 (페이퍼) =====")
    print(f"청산 {r['closes']}건 (승 {r['wins']}/패 {r['losses']}) | 부분익절 {r['trims']}건")
    if r["closes"]:
        print(f"승률 {r['winRate']}% | 총실현 {r['totalRealized']:+}pt | "
              f"기대값 {r['expectancy']:+}pt/건")
        print(f"평균승 {r['avgWin']} | 평균패 {r['avgLoss']} | PF {r['profitFactor']}")
        for sig, d in sorted(r["bySignal"].items()):
            print(f"   {sig:11} {d['n']}건  실현 {d['pl']:+}pt")   # 어떤 규칙이 돈을 벌었나
    else:
        print("청산 이력 없음")
    return 0


def cmd_undo() -> int:
    bak = STATE.with_suffix(".json.bak")
    if not bak.exists():
        print("⛔ 백업 없음"); return 1
    shutil.copy2(bak, STATE)
    print("복원 완료 (직전 상태로):", STATE)
    return 0


# ------------------------------------------------------------------- cycle
def main():
    global _DATA_FAILS
    _DATA_FAILS = 0
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    sess = us_session(now)
    print(f"===== 시장 사이클 {now.strftime('%Y-%m-%d %H:%M KST')} · {sess} =====")

    st = load_state()
    # #36 loop health: detect missed cycles
    try:
        if st.get("lastRunAt"):
            gap = (now - datetime.fromisoformat(st["lastRunAt"])).total_seconds()
            if gap > 45 * 60:
                print(f"  ⚠️ 루프 갭 {int(gap/60)}분 — 사이클 누락 의심")
    except Exception:
        pass
    st["lastRunAt"] = now.isoformat(timespec="seconds")
    if st.get("_integrity") == "MISMATCH":               # #39
        print("  ⚠️ 상태 체크섬 불일치 — 수동 수정/오염 가능성, 검토 필요")
        journal({"event": "ALERT", "kind": "state_integrity"})

    for w in RM.validate_config(load_config().public_summary()):
        print("  ", w)

    raw_reg = ST.assess_regime(snap)
    eff = effective_regime(st, raw_reg["regime"])        # #3
    dep = ST.DEPLOY.get(eff, 0.6)
    prev = st.get("lastRegime")
    flip = prev and prev != eff
    pend = f" (원시 {raw_reg['regime']}, 확정대기)" if eff != raw_reg["regime"] else ""
    print(f"[REGIME] {eff.upper()}{pend} | 지수 MA20위 {raw_reg['indexAboveMA20']} | "
          f"배치비율 {int(dep*100)}%" + (f"   🔔 전환 {prev}→{eff}!" if flip else ""))
    for d in raw_reg["detail"]:
        print("   ", d)
    if flip:
        journal({"event": "REGIME_FLIP", "from": prev, "to": eff})   # #45

    print(f"\n[페이퍼 포지션] {len(st['positions'])}종목 | 현금 {int(st['cashFrac']*100)}% "
          f"| 실현누적 {st.get('realizedPlPct', 0.0):+.2f}pt "
          f"| 오늘 진입 {st['dailyStats']['entries']}/{MAX_ENTRIES_PER_DAY}"
          f"·손실청산 {st['dailyStats']['lossCloses']}/{MAX_LOSS_CLOSES_PER_DAY}")
    changes, open_contrib = [], 0.0
    for p in st["positions"]:
        # 미체결(pendingFill) 실포지션은 신호 판정에서 제외 — 체결/취소가 확정되어야
        # entryRef/qty가 유효하다(live_trade --status가 reconcile). 스킵.
        if p.get("pendingFill") or p.get("entryRef") is None:
            print(f"   {p.get('symbol','?'):6} {'PENDING':10} 체결 확정 대기(신호 판정 제외)")
            continue
        # 포지션별 격리: 하나의 불량 레코드가 나머지 포지션의 손절 관리를 죽이지 않게.
        try:
            e = evaluate_position(p, today=now.date())
        except Exception as ex:                          # noqa: BLE001
            print(f"   {p.get('symbol','?'):6} {'ERR':10} 평가실패 {type(ex).__name__} — 스킵")
            journal({"event": "ALERT", "kind": "eval_error",
                     "symbol": p.get("symbol"), "err": type(ex).__name__})
            continue
        p["highwater"] = e.get("highwater", p.get("highwater"))
        flag = "" if e["signal"] in ("HOLD", "DATA?") else "  ⚠️변경신호"
        print(f"   {e['symbol']:6} {e['signal']:10} {e['reason']}{flag}")
        if e["signal"] not in ("HOLD", "DATA?"):
            changes.append(e)
        if e.get("plPct") is not None:
            open_contrib += p.get("weight", 0.0) * e["plPct"]
            if e["plPct"] <= ALERT_DROP_PCT:             # 급락 이상징후 (갭으로 손절선 관통)
                print(f"   🚨 URGENT {e['symbol']} {e['plPct']}% — 손절선 갭 관통, 즉시 검토")
                journal({"event": "ALERT", "kind": "gap_drop",
                         "symbol": e["symbol"], "plPct": e["plPct"]})

    equity, daily, dd, ok, why = equity_and_circuit(st, round(open_contrib, 3), today)
    print(f"\n[리스크] 총손익 {equity:+.2f}pt (실현 {st.get('realizedPlPct',0):+.2f} / 평가 "
          f"{open_contrib:+.2f}) | 오늘 {daily:+.2f}pt | 낙폭 {dd:+.2f}pt | "
          f"서킷 {'OK' if ok else '🛑TRIP'}")
    if not ok:
        print("  ", why)
        if "일일손실" in why:                          # #70: 다음날 비중 반감 예약
            st["reduceUntil"] = (now + timedelta(days=1)).strftime("%Y-%m-%d")
            print(f"   → 내일({st['reduceUntil']}) 신규 비중 자동 반감")
    # 마감 1시간 전(04:00~05:00 KST) 야간 익스포저 점검 (#66)
    if sess == "미국 정규장" and dtime(4, 0) <= now.time() < dtime(5, 0):
        dep_w = sum(p.get("weight", 0) for p in st["positions"])
        print(f"   ⏰ 미국장 마감 1시간 전 — 야간 보유 익스포저 {dep_w:.0%} (신규 진입 비권장)")

    # #6 session-aware scan cache: during 휴장 US candles don't move — reuse.
    if sess == "미국장 휴장" and st.get("lastScan"):
        sc = {"candidates": st["lastScan"].get("candidates", []), "rejected": []}
        cache_note = " (휴장 — 직전 스캔 재사용)"
    else:
        sc = ST.scan(S.CLIENT)
        st["lastScan"] = {"ts": now.isoformat(timespec="seconds"),
                          "candidates": sc["candidates"]}
        cache_note = ""
    held = {p["symbol"] for p in st["positions"]}
    fresh = [c for c in sc["candidates"] if c["symbol"] not in held]
    print(f"\n[신규 리더 후보] {len(fresh)}종목 (미보유){cache_note}")
    for i, c in enumerate(fresh[:5]):
        can, rr = RM.check_new_entry(st["positions"], st["cashFrac"], c["symbol"], 0.2, dep)
        cool = st["cooldowns"].get(c["symbol"])
        blocked = (not can and rr) or (cool and today < cool and f"쿨다운~{cool}") \
            or (not ok and "서킷") or (eff == "risk_off" and "risk_off")
        tag = "✅편입가능" if not blocked else f"⛔{blocked}"
        extra = (f" RS {c.get('rs20')}" if c.get("rs20") is not None else "") + \
                (f" 갭{c.get('gapPct')}%" if c.get("gapPct") else "")
        odata = ""
        if i < 3 and sess != "미국장 휴장":
            _, age = price_meta(c["symbol"])
            ok3, why3 = RM.check_order_data(age, spread_pct(c["symbol"]),
                                            ST.CAPITAL_KRW * 0.2)
            odata = " | 주문가드 OK" if ok3 else f" | ⛔{why3}"
        print(f"   [{c['tier']}] {c['symbol']:6} last {c['last']} RSI {c['rsi']} "
              f"MA20+{c['extPct']}% ATR {c.get('atrPct')}%{extra} {tag}{odata}")

    print("\n[변경 판단 요약]")
    if not ok:
        print("   → 🛑 서킷 발동: 신규진입 중단, 리스크 축소")
    for c in changes:
        print(f"   → {c['symbol']}: {c['signal']} ({c['reason']})"
              f"  ⇒ `market_cycle.py --signal {c['symbol']} {c['signal']}`")
    if eff == "risk_off":
        print(f"   → regime risk_off: 신규 편입 자제, 배치 {int(dep*100)}%")
    if not changes and ok:
        print("   → 기존 포지션 변경신호 없음 (HOLD)")
    if _DATA_FAILS:
        print(f"   ⚠️ 데이터 조회실패 {_DATA_FAILS}건 (품질 저하 주의)")   # #37

    st["lastRegime"] = eff
    # N2: 느린 스캔 동안 락을 쥐지 않는다(그 사이 live_trade의 실매도 STOP이 굶지
    # 않도록). 저장 순간에만 짧게 락을 잡고, 최신 상태를 다시 읽어 '이번 사이클이
    # 계산한 highwater·북키핑'만 덧씌워 저장(lost-update 없이 starvation 없이).
    _lk = _acquire_lock(block=True, timeout=15.0)
    if _lk is None:
        print("   ⚠️ 저장 락 실패(15s) — 이번 사이클 상태 미저장(관찰만)")
    else:
        try:
            fresh = load_state()
            hw = {p["symbol"]: p.get("highwater") for p in st["positions"]
                  if not p.get("pendingFill") and p.get("highwater") is not None}
            for p in fresh["positions"]:
                if not p.get("pendingFill") and p["symbol"] in hw:
                    try:
                        p["highwater"] = max(float(p.get("highwater") or 0),
                                             float(hw[p["symbol"]]))
                    except (TypeError, ValueError):
                        pass
            for k in ("lastRunAt", "lastRegime", "regimeStreak", "dayStart",
                      "peakEquityPct", "lastScan", "reduceUntil"):
                if k in st:
                    fresh[k] = st[k]
            journal({"event": "SNAPSHOT", "regime": eff, "equityPct": equity,
                     "dailyPct": daily, "drawdownPct": dd, "dataFails": _DATA_FAILS,
                     "positions": len(fresh["positions"]),
                     "changes": [c["symbol"] for c in changes]})
            save_state(fresh)
        finally:
            try:
                _lk.close()
            except Exception:
                pass
    print("\n(판단은 에이전트가 위 데이터로 최종 결정. 실주문 아님 — 페이퍼.)")


USAGE = """market_cycle.py — 30분 시장 사이클 (페이퍼/실전 겸용 판단 엔진)
  (없음)            사이클 리포트 (regime·포지션신호·리스크·후보)
  --apply '<json>'  상태 교체(검증+백업)   --undo   직전 변경 복원
  --enter SYM W     페이퍼 진입            --signal SYM SIG  페이퍼 청산/익절
  --report          성과 리포트(승률·PF·시그널별)
실주문은 live_trade.py 사용. 긴급정지: touch ~/.toss-mcp/HALT"""


def _acquire_lock(block: bool = False, timeout: float = 10.0):
    """#41 다중 프로세스 락 — cron 사이클·live_trade 주문·수동 실행이 상태파일을
    동시에 쓰지 못하게 직렬화(lost-update 방지). live_trade가 같은 파일을 잡는다.
    block=True면 timeout까지 대기. **획득 실패 시 반드시 None(fail-closed)** —
    예외를 삼켜 가짜 핸들을 돌려주면 락 없이 실주문이 진행돼 lost-update가 재발한다."""
    try:
        import fcntl
    except Exception:
        return None                    # 락 불가 플랫폼 → 잠금 없이 매매 금지(fail-closed)
    try:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        lf = open(STATE.parent / "cycle.lock", "w")
    except Exception:
        return None
    deadline = time.time() + timeout
    while True:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return lf              # keep ref alive (flock releases on close/exit)
        except OSError:
            if not block or time.time() > deadline:
                try:
                    lf.close()
                except Exception:
                    pass
                return None
            time.sleep(0.3)


def _locked_run(fn):
    """빠른 상태변이 서브커맨드용: 짧게 락 잡고 실행(락 실패 시 중단)."""
    lk = _acquire_lock(block=True, timeout=20.0)
    if lk is None:
        print("⛔ 다른 사이클/주문 실행 중 (cycle.lock, 20s 초과) — 재시도")
        return 1
    try:
        return fn()
    finally:
        try:
            lk.close()
        except Exception:
            pass


def _apply(cand):
    errs = validate_state(cand)
    if errs:
        print("⛔ 상태 검증 실패:")
        for e in errs:
            print("   -", e)
        return 1
    base = load_state()
    base.update(cand)
    for p in base["positions"]:
        p.setdefault("entryDate", datetime.now(KST).strftime("%Y-%m-%d"))
        p.setdefault("highwater", p.get("entryRef"))
        p.setdefault("tp1_done", False)
    save_state(base)
    print("paper state 저장(검증 통과):", STATE)
    return 0


if __name__ == "__main__":
    argv = sys.argv
    if len(argv) > 1 and argv[1] in ("--help", "-h"):
        print(USAGE)
        sys.exit(0)
    # 빠른 변이 서브커맨드는 짧은 락 안에서. main()·--report는 자체적으로 처리
    # (main()은 느린 스캔 동안 락을 쥐지 않고, 저장 순간에만 짧게 잡는다 → N2).
    if len(argv) > 2 and argv[1] == "--apply":
        sys.exit(_locked_run(lambda: _apply(json.loads(argv[2]))))
    elif len(argv) > 3 and argv[1] == "--enter":
        sys.exit(_locked_run(lambda: cmd_enter(argv[2], float(argv[3]))))
    elif len(argv) > 3 and argv[1] == "--signal":
        sys.exit(_locked_run(lambda: cmd_signal(argv[2], argv[3])))
    elif len(argv) > 1 and argv[1] == "--report":
        sys.exit(cmd_report())
    elif len(argv) > 1 and argv[1] == "--undo":
        sys.exit(_locked_run(cmd_undo))
    else:
        main()
