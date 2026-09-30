"""stock_screener.py — 넓은 유니버스 리서치·스크리너 (내가 담당).

S&P500 우량주 ~40종(섹터 분산)을 매일 훑어, '안정 지향 역추세 스윙'에 맞는
종목을 골라 랭킹한다. 고정 바구니가 아니라 '지금 조건 맞는 것'을 동적으로 선별.

선별 기준(안정성 우선):
  · 장기 추세 살아있음   : 종가 > 150일선 (구조적 상승만)
  · 지금 눌림 = 매수 존   : RSI 38~58 (과열 아님, 살짝 눌린)
  · 저변동(안정)         : ATR% 낮을수록 가점
  · 비과열               : 50일선 대비 +8% 이내 (끝물 추격 금지)
  · 신선도               : 데이터 정상
점수 = 추세 + 눌림적합 − 변동성 − 과열. 상위 N개 출력.
⚠️ 매수 직전 '실적일 임박' 수동 확인 필요(스크리너는 시세만 봄).

Usage: python3.12 stock_screener.py [top]   (기본 상위 10)
"""
from __future__ import annotations
import sys, time, json

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S       # noqa: E402
from execute import SECTORS  # noqa: E402  (D2 섹터 태그 — 단일 진실 재사용)
import indicators as IND  # noqa: E402
import trading_rules as TR  # noqa: E402

# 넓은 S&P500 우량주 유니버스 (섹터 대폭 분산, ~75종)
UNIVERSE = [
    # 빅테크/플랫폼
    "AAPL", "MSFT", "GOOGL", "META", "AMZN", "NFLX", "DIS", "TMUS", "CMCSA",
    # 반도체
    "NVDA", "AVGO", "AMD", "QCOM", "TXN", "MU", "LRCX", "KLAC", "AMAT", "ARM",
    # 소프트웨어
    "ORCL", "CRM", "ADBE", "NOW", "INTU", "PANW",
    # 소비/유통
    "COST", "WMT", "HD", "LOW", "MCD", "NKE", "SBUX", "TJX", "BKNG", "CMG", "TGT",
    # 필수소비/방어
    "PG", "KO", "PEP", "PM", "MO", "MDLZ", "CL",
    # 헬스케어/바이오
    "LLY", "UNH", "JNJ", "ABBV", "MRK", "TMO", "ABT", "ISRG", "VRTX", "REGN", "BMY", "GILD", "ELV",
    # 금융/결제
    "JPM", "BAC", "WFC", "C", "MS", "GS", "V", "MA", "AXP", "SCHW", "BLK", "SPGI",
    # 산업/방산
    "CAT", "GE", "HON", "DE", "LMT", "RTX", "UNP", "ADP",
    # 에너지/유틸
    "XOM", "CVX", "COP", "SLB", "NEE",
]

# 실보유(레거시) 종목은 전략 유니버스에서 제외 — 실계좌분과 병합/오염 방지
LEGACY = {"AAPL", "TSLA", "MBRX", "000660", "005930"}
UNIVERSE = [s for s in UNIVERSE if s not in LEGACY]

# 2단 스크리닝(사용자 제안 2026-07-20): S&P500 전체는 하루 1회 딥스캔으로 추리고,
# 30분 사이클은 그 결과(동적 유니버스 80)만 본다. 위 UNIVERSE는 폴백(정적)용.
SP500_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sp500_tickers.txt")
DYN_FILE = "~/.toss-trader/universe_dynamic.json"
DYN_FRESH_HOURS = 36     # 이보다 오래되면 정적 폴백(딥스캔 크론 사망 대비)


def _load_universe():
    """30분 사이클용 유니버스: 동적(데일리 딥스캔 결과, 신선할 때) → 정적 폴백.
    (symbols, source_label) 반환."""
    import os
    from datetime import datetime as _dt
    try:
        p = os.path.expanduser(DYN_FILE)
        if os.path.exists(p):
            d = json.loads(open(p, encoding="utf-8").read())
            ts = _dt.fromisoformat(str(d.get("ts"))[:19])
            age_h = (_dt.now() - ts).total_seconds() / 3600
            syms = [s for s in (d.get("symbols") or []) if s not in LEGACY]
            if syms and age_h <= DYN_FRESH_HOURS:
                return syms, f"동적(딥스캔 {str(d.get('ts'))[:16]}, S&P500→{len(syms)})"
            if syms:
                return ([s for s in UNIVERSE],
                        f"정적 폴백(동적 {age_h:.0f}h 경과 — 딥스캔 크론 확인 필요)")
    except Exception:
        pass
    return [s for s in UNIVERSE], "정적(딥스캔 이력 없음)"


def daily_scan(top_n=80):
    """S&P500 전체(~500) 하루 1회 딥스캔 → 상위 top_n을 동적 유니버스로 저장.
    analyze()의 기존 필터(추세·유동성$50M·신선도·155봉)를 전 종목에 적용하고 점수순 선별.
    30분 사이클은 이 결과만 봐서 '전체를 보되 API는 아낀다'(사용자 제안)."""
    import os
    import time as _t
    from datetime import datetime as _dt
    try:
        allsyms = [x.strip() for x in open(SP500_FILE, encoding="utf-8") if x.strip()]
    except Exception as e:
        print("S&P500 목록 없음:", e); return 1
    allsyms = [s for s in allsyms if s not in LEGACY]
    if _is_us_regular_now():      # 감사#228: 개장 중 498건 연속조회는 레이트리밋에 걸려 열화된다
        print("⚠️ 미국 정규장 중 딥스캔 — API 혼잡으로 조회실패↑ 가능(장마감 후 실행 권장)")
    print(f"S&P500 딥스캔 시작: {len(allsyms)}종목 (하루 1회 · 상위 {top_n} 추출)")
    rows, fails = [], 0
    delay, streak = 0.15, 0        # 적응형 스로틀(감사#228): 연속실패 시 감속, 회복 시 가속
    for i, sym in enumerate(allsyms, 1):
        try:
            r = analyze(sym)
            if r and r.get("up"):
                rows.append(r); streak = 0
            elif r is None:
                fails += 1; streak += 1
            else:
                streak = 0          # 분석은 됐고 추세만 미달 — 정상
        except Exception:
            fails += 1; streak += 1
        if streak >= 5:             # 연속 5회 조회실패 = 레이트리밋 신호 → 감속 + 쿨다운
            delay = min(delay * 2, 2.0)
            print(f"  ⏳ 연속실패 {streak} — 레이트리밋 의심, 간격 {delay:.2f}s로 감속 후 15s 대기")
            _t.sleep(15); streak = 0
        elif streak == 0 and delay > 0.15:
            delay = max(0.15, delay * 0.9)      # 회복되면 서서히 원복
        if i % 50 == 0:
            print(f"  …{i}/{len(allsyms)} (통과 {len(rows)} 실패 {fails} 간격 {delay:.2f}s)")
        _t.sleep(delay)
    cov = len(rows) / max(1, len(allsyms))
    if len(rows) < top_n or cov < 0.30:      # 품질 하한(감사#223): 열화 스캔은 저장 거부
        print(f"⛔ 딥스캔 품질 미달(통과 {len(rows)} < {top_n} 또는 커버리지 {cov*100:.0f}%<30%)"
              f" — 기존 유니버스 유지(덮어쓰기 안 함)")
        return 1
    rows.sort(key=lambda r: -r["score"])
    top = rows[:top_n]
    syms = [r["sym"] for r in top]
    prev = []
    p = os.path.expanduser(DYN_FILE)
    try:
        if os.path.exists(p):
            prev = json.loads(open(p, encoding="utf-8").read()).get("symbols") or []
    except Exception:
        pass
    try:
        tmp = p + ".tmp"
        open(tmp, "w", encoding="utf-8").write(json.dumps(
            {"ts": _dt.now().isoformat(timespec="seconds"), "n_scanned": len(allsyms),
             "n_passed": len(rows), "symbols": syms,
             "top": [{"sym": r["sym"], "score": r["score"], "rsi": r["rsi"],
                      "zone": r["zone"], "px": r["last"]} for r in top]},
            ensure_ascii=False))
        os.replace(tmp, p)
    except Exception as e:
        print("동적 유니버스 저장 실패:", e); return 1
    new = [s for s in syms if s not in prev]
    gone = [s for s in prev if s not in syms]
    print(f"\n완료: 스캔 {len(allsyms)} → 필터통과 {len(rows)} → 상위 {len(syms)} 저장(실패/제외 {fails})")
    if prev:
        print(f"Δ 유니버스: 신규 {len(new)}개 {new[:10]}{'…' if len(new) > 10 else ''}")
        print(f"           제외 {len(gone)}개 {gone[:10]}{'…' if len(gone) > 10 else ''}")
    print(f"상위 10: {', '.join(r['sym'] for r in top[:10])}")
    return 0


_CANDLE_CACHE_DIR = None


def _fetch(sym, need=200):
    """일봉 fetch + 디스크 캐시 TTL 20분(D7): 30분 사이클 간 재실행·수동 재조회 시
    API 호출 절감(레이트리밋 완화). 캐시 손상/만료 시 자연스레 재조회."""
    import os as _os
    global _CANDLE_CACHE_DIR
    if _CANDLE_CACHE_DIR is None:
        _CANDLE_CACHE_DIR = _os.path.expanduser("~/.toss-trader/cache/candles")
        try:
            _os.makedirs(_CANDLE_CACHE_DIR, exist_ok=True)
        except Exception:
            pass
    cf = _os.path.join(_CANDLE_CACHE_DIR, f"{sym}.json")
    try:
        if _os.path.exists(cf) and (time.time() - _os.path.getmtime(cf)) < 1200:
            c = json.loads(open(cf, encoding="utf-8").read())
            # 세션 경계 무효화(감사#223): 개장 전 캐시(오늘봉 없음)를 개장 후 20분간 재사용하면
            # 갭·당일변동 판정이 눈먼다. ET 오늘봉 유무가 캐시와 현재가 다르면 재조회.
            if c and len(c) >= 155:
                cached_has_today = str(c[-1].get("timestamp"))[:10] == _et_today()
                stale_session = (not cached_has_today) and _is_us_regular_now()
                if not stale_session:
                    return c
    except Exception:
        pass
    delay = 1.0
    for _ in range(3):
        try:
            c = S.CLIENT.get_candles(sym, "1d", need).get("candles", [])
            if c and len(c) >= 155:
                try:
                    tmp = cf + ".tmp"
                    open(tmp, "w", encoding="utf-8").write(json.dumps(c))
                    _os.replace(tmp, cf)
                except Exception:
                    pass
                return c
        except Exception:
            time.sleep(delay); delay *= 2      # 지수백오프(SC12): 1→2→4s, 상관실패 완화
    return None


def _is_us_regular_now():
    """지금이 미국 정규장(ET 평일 09:30~16:00)인지 — 캐시 세션경계 무효화 판정용(감사#223).
    zoneinfo 없으면 False(보수적: 캐시 유지)."""
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        n = _dt.now(ZoneInfo("America/New_York"))
    except Exception:
        return False
    if n.weekday() >= 5:
        return False
    m = n.hour * 60 + n.minute
    return 570 <= m < 960          # 09:30 ~ 16:00 ET


def _ma(closes, n):
    return sum(closes[-n:]) / n if len(closes) >= n else None


def _et_today():
    """미국 동부 오늘 날짜(YYYY-MM-DD). 최신봉이 '오늘 진행중 세션'인지 판정용(SL14)."""
    from datetime import datetime as _dt
    try:
        from zoneinfo import ZoneInfo
        return _dt.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
    except Exception:
        from datetime import timedelta as _td
        now = _dt.utcnow()
        off = 4 if 3 <= now.month <= 11 else 5      # 월기반 EDT/EST 근사(G7)
        return (now - _td(hours=off)).strftime("%Y-%m-%d")


def _partial_drop(latest_day, et_today, n_bars):
    """최신봉을 '부분봉'으로 제외할지 판정: 최신봉 날짜==오늘(ET)이고 봉수 충분(≥155)일 때만 True.
    장 마감·오늘봉 미생성이면 최신봉은 완료된 전일봉 → 버리지 않음(SL14: 완료봉 손실 방지)."""
    return (latest_day == et_today) and (n_bars >= 155)


def _days_to_earn(sym):
    """실적일까지 남은 일수(execute와 같은 ~/.toss-trader/earnings_dates.json 직접 읽음,
    import 순환 회피). 알려진 것만·모르면 None."""
    import json
    import os
    from datetime import date
    try:
        f = os.path.expanduser("~/.toss-trader/earnings_dates.json")
        if not os.path.exists(f):
            return None
        with open(f, encoding="utf-8") as fh:
            e = json.load(fh).get(sym.upper())
        if not e:
            return None
        y, m, d = map(int, str(e)[:10].split("-"))
        from datetime import datetime as _dt
        try:                                    # execute와 동일 ET 거래일 기준(SC1: KST 하루 어긋남 방지)
            from zoneinfo import ZoneInfo
            today = _dt.now(ZoneInfo("America/New_York")).date()
        except Exception:
            today = date.today()
        return (date(y, m, d) - today).days
    except Exception:
        return None


_IDX_CACHE = {"done": False, "ret": None, "ret1m": None}


def _qqq_rets():
    """QQQ 3개월(63봉)·1개월(21봉) 수익률 — RS 다중기간 기준(D4). 런당 1회 조회·캐시(SC5)."""
    if _IDX_CACHE["done"]:
        return _IDX_CACHE["ret"], _IDX_CACHE["ret1m"]
    _IDX_CACHE["done"] = True
    try:
        c = _fetch("QQQ")
        if c:
            cl = [float(x["close"]) for x in sorted(c, key=lambda r: str(r["timestamp"]))]
            if len(cl) >= 64:
                _IDX_CACHE["ret"] = (cl[-1] / cl[-64] - 1) * 100
            if len(cl) >= 22:
                _IDX_CACHE["ret1m"] = (cl[-1] / cl[-22] - 1) * 100
    except Exception:
        pass
    if _IDX_CACHE["ret"] is None:                # RS 비활성 명시(SC6/18: 조용히 꺼지는 것 방지)
        print("※ 상대강도(RS) 비활성 — QQQ 조회 실패(이번 런 RS=0)")
    return _IDX_CACHE["ret"], _IDX_CACHE["ret1m"]


def _rs_blend(ret3m, ret1m, qqq3, qqq1):
    """RS 다중기간 합성(D4): 3개월 70% + 1개월 30% — 장기 추세강도에 최근 자금흐름 가미.
    한쪽 결측이면 있는 쪽만(가중 재정규화). 둘 다 없으면 0."""
    parts = []
    if ret3m is not None and qqq3 is not None:
        parts.append((ret3m - qqq3, 0.7))
    if ret1m is not None and qqq1 is not None:
        parts.append((ret1m - qqq1, 0.3))
    if not parts:
        return 0.0
    wsum = sum(w for _, w in parts)
    return sum(v * w for v, w in parts) / wsum


def analyze(sym):
    c = _fetch(sym)
    if not c:
        return None
    c = sorted(c, key=lambda x: str(x.get("timestamp")))     # 오름차순 보장
    try:                                                     # 신선도(D10/SC13): 최신봉 오래되면 제외
        from datetime import datetime as _dt, timedelta as _td
        ld = _dt.fromisoformat(str(c[-1].get("timestamp")).replace("Z", "+00:00"))
        now = _dt.now(ld.tzinfo) if ld.tzinfo else _dt.now()
        if (now - ld) > _td(days=5):                         # 5일↑ = 데이터갭/거래정지/상폐 의심
            return None
    except Exception:
        pass
    closes_all = [float(x["close"]) for x in c if x.get("close") is not None]
    if len(closes_all) < 155:
        return None
    last = closes_all[-1]                       # 실시간(진행중 부분봉) 종가 = 현재가(rank20: 정수반올림 대신 실 USD)
    # 지표는 '완료봉'만으로(진행중 부분봉 제외) → 30분 사이클간 선별 재현성 확보(rank20).
    # 단 오늘(ET) 봉일 때만 부분봉으로 간주해 제외 — 장 마감시 완료 전일봉은 유지(SL14/exec학습).
    c_done = c[:-1] if _partial_drop(str(c[-1].get("timestamp"))[:10], _et_today(),
                                     len(closes_all)) else c
    closes = [float(x["close"]) for x in c_done if x.get("close") is not None]
    ind = IND.compute(c_done)
    rsi = ind.get("rsi14")
    ma150 = _ma(closes, 150); ma50 = _ma(closes, 50)
    atr = TR.atr_pct(c_done, 14)
    if any(v is None for v in (last, rsi, ma150, ma50, atr)) or last <= 0:
        return None                                    # 0을 결측으로 오인 방지(SC11: 명시적 None체크)
    try:                                               # 유동성 하한(SC1): 20일 중앙 거래대금 낮으면 제외
        dvol = sorted(float(x.get("close") or 0) * float(x.get("volume") or 0) for x in c_done[-20:])
        if dvol and dvol[len(dvol) // 2] < 50_000_000:  # 중앙값 $50M 미만 = 얇음(D3 상향: 슬리피지 함정)
            return None
    except Exception:
        pass
    up = last > ma150                                  # 장기 추세
    ext50 = (last / ma50 - 1) * 100                    # 50일선 대비 확장
    ret6m = round((last / closes[-126] - 1) * 100, 1) if len(closes) >= 126 else None
    ret20 = (last / closes[-21] - 1) * 100 if len(closes) >= 21 else 0.0    # 최근 20일 수익률
    knife = ret20 < -12                                # 20일 -12%↓ = 급락(falling-knife)
    gap = 0.0; gapped = False                          # 당일 갭(오늘 시가 vs 어제 종가, SC10)
    try:
        gap = (float(c[-1]["open"]) / float(c[-2]["close"]) - 1) * 100
        gapped = abs(gap) > 3                          # |갭|>3% = 눌림 아님(제외)
    except Exception:
        gapped = True                                 # 갭 계산 실패 → fail-closed(안전 제외)
    vols = [float(x.get("volume") or 0) for x in c_done]    # 볼륨확인(SC2)
    vol_ratio = None
    if len(vols) >= 21 and sum(vols[-21:-1]) > 0:
        vol_ratio = vols[-1] / (sum(vols[-21:-1]) / 20)     # 최근 완료봉 vs 20일평균
    dte = _days_to_earn(sym)                           # 실적 임박도(SC8)
    earn_bonus = 3 if (dte is not None and 5 <= dte <= 12) else 0    # 기대감 윈도우 가점(안정전략이라 소폭, SC4)
    near_earn = dte is not None and 0 <= dte <= 4      # 발표 임박(4일내) = 매수존 제외(3~4일 갭위험, SC5)
    ret3m = (last / closes[-63] - 1) * 100 if len(closes) >= 63 else None
    ret1m = (last / closes[-21] - 1) * 100 if len(closes) >= 21 else None
    qqq3, qqq1 = _qqq_rets()                           # 상대강도 다중기간(D4): 3m 70%+1m 30%
    rs = _rs_blend(ret3m, ret1m, qqq3, qqq1)
    # 스윙 매수 적합도(눌림): RSI 38~58 최고, 밖으로 갈수록 감점
    pull = 20 - abs(rsi - 48)                          # 48 근처 최고
    score = ((25 if up else -50)                       # 추세 필수
             + pull                                    # 눌림 적합
             - atr * 2.5                               # 저변동 선호
             - max(0, ext50 - 8) * 2                   # 끝물 감점
             - (15 if knife else 0)                    # 급락(knife) 감점(SC7)
             + earn_bonus                              # 실적 기대감 윈도우 가점(SC8)
             + max(-8, min(8, rs * 0.2))               # 상대강도 가점/감점(±8 캡, SC5)
             - (8 if gapped else 0)                    # 당일 큰 갭 감점(SC10)
             + (3 if (vol_ratio is not None and vol_ratio < 0.9) else 0)    # 조용한 눌림 가점(SC2)
             - (5 if (vol_ratio is not None and vol_ratio > 1.5) else 0))   # 대량 매도 감점(SC2)
    rr = round(rsi)                                    # 표시·게이트 동일값(CX20 경계 불일치 방지)
    zone = ("🟢매수존" if (up and 38 <= rr <= 58 and ext50 <= 8 and not knife
                        and not near_earn and not gapped) else
            ("⚪관망" if up else "🔴추세깨짐"))
    return {"sym": sym, "last": last, "rsi": rr, "atr": atr,
            "up": up, "ext50": round(ext50, 1), "ret6m": ret6m,
            "score": round(score, 1), "zone": zone}


def _load_earnings_days(path="~/.toss-trader/earnings_dates.json"):
    """실적일 파일 → {SYM: 남은일수}(D1: 표에서 실적임박 즉시 확인 — 별도 수동체크 감소).
    ET 오늘 기준. 파싱 실패·과거일은 제외."""
    import os
    from datetime import date, datetime as _dt
    out = {}
    try:
        from zoneinfo import ZoneInfo
        today = _dt.now(ZoneInfo("America/New_York")).date()
    except Exception:
        today = date.today()
    try:
        p = os.path.expanduser(path)
        if os.path.exists(p):
            for k, v in json.loads(open(p, encoding="utf-8").read()).items():
                try:
                    d = date.fromisoformat(str(v)[:10])
                    if d >= today:
                        out[str(k).upper()] = (d - today).days
                except Exception:
                    continue
    except Exception:
        pass
    return out


def _diff_and_snapshot(buy, top_rows,
                       path="~/.toss-trader/screener_last.json"):
    """사이클 간 매수존 diff(D10) + 선별 스냅샷 저장(D8: 재현성·사후검증).
    직전 실행의 매수존과 비교해 '신규 진입/이탈'을 명시 — 셋업 변화를 사람이 놓치지 않게."""
    import os
    from datetime import datetime as _dt
    p = os.path.expanduser(path)
    now_set = sorted({r["sym"] for r in buy})
    prev_set = []
    try:
        if os.path.exists(p):
            prev = json.loads(open(p, encoding="utf-8").read())
            prev_set = prev.get("buyzone") or []
    except Exception:
        pass
    if prev_set:
        new = [s for s in now_set if s not in prev_set]
        gone = [s for s in prev_set if s not in now_set]
        if new or gone:
            print(f"Δ 직전 대비: 신규 매수존 {', '.join(new) or '없음'} | "
                  f"이탈 {', '.join(gone) or '없음'}")
        else:
            print("Δ 직전 대비: 매수존 변동 없음")
    try:      # 스냅샷(D8): 상위표 점수·가격 포함 저장(임시파일→rename 원자적)
        snap = {"ts": _dt.now().isoformat(timespec="seconds"), "buyzone": now_set,
                "top": [{"sym": r["sym"], "score": r["score"], "rsi": r["rsi"],
                         "zone": r["zone"], "px": r["last"]} for r in top_rows]}
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False))
        os.replace(tmp, p)
        hist = p.replace(".json", "_history.jsonl")   # 픽 성과추적용 적립(D9 데이터 기반)
        if os.path.exists(hist) and os.path.getsize(hist) > 5_000_000:
            os.replace(hist, hist + ".1")
        with open(hist, "a", encoding="utf-8") as f:
            f.write(json.dumps(snap, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _review_picks(history_rows, prices_now, min_age_days=3):
    """픽 성과 판정(D9 실용판, 순수함수): min_age_days 이상 지난 가장 오래된 스냅샷의
    매수존 픽들을 현재가와 비교 → [(sym, then_px, now_px, ret_pct)], 스냅샷 ts.
    '스크리너가 고른 종목이 실제로 올랐는가'를 측정 — 점수 가중치 조정의 근거 데이터."""
    from datetime import datetime as _dt, timedelta
    if not history_rows:
        return [], None
    cut = _dt.now() - timedelta(days=min_age_days)
    old = [h for h in history_rows
           if _dt.fromisoformat(str(h.get("ts"))[:19]) <= cut]
    snap = old[-1] if old else history_rows[0]         # 컷오프 이전 중 '가장 최신'(감사#223)
    px_then = {t["sym"]: float(t.get("px") or 0) for t in (snap.get("top") or [])
               if t.get("zone") == "🟢매수존"}
    out = []
    for sym, then in px_then.items():
        now = prices_now.get(sym)
        if then > 0 and now:
            out.append((sym, then, float(now), (float(now) / then - 1) * 100))
    return out, snap.get("ts")


def review(min_age_days=3):
    """--review: 과거 스냅샷의 매수존 픽 vs 현재가 — 스크리너 적중률 학습 리포트(READ-ONLY)."""
    import os
    p = os.path.expanduser("~/.toss-trader/screener_last_history.jsonl")
    print("=" * 58); print(f"스크리너 픽 성과 리뷰 ({min_age_days}일 전 기준 · READ-ONLY)"); print("=" * 58)
    rows = []
    try:
        for line in open(p, encoding="utf-8"):
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except FileNotFoundError:
        print("히스토리 없음 — 스크리너가 몇 사이클 돌면 쌓입니다"); return 0
    syms = set()
    for h in rows:
        for t in (h.get("top") or []):
            syms.add(t["sym"])
    prices = {}
    for s in sorted(syms):
        try:
            prices[s] = float(S.CLIENT.get_price(s)["lastPrice"])
        except Exception:
            continue
    res, ts = _review_picks(rows, prices, min_age_days)
    if not res:
        print(f"비교 가능한 픽 없음(스냅샷 {len(rows)}개 적립 중)"); return 0
    res.sort(key=lambda r: -r[3])
    win = sum(1 for r in res if r[3] > 0)
    print(f"기준 스냅샷: {str(ts)[:16]} | 픽 {len(res)}개")
    for sym, then, now, ret in res:
        print(f"  {sym:6} {then:9.2f} → {now:9.2f}  {ret:+.1f}%")
    print(f"\n적중률(+): {win}/{len(res)} ({win / len(res) * 100:.0f}%) | "
          f"평균 {sum(r[3] for r in res) / len(res):+.2f}%")
    print("※ 낮으면 점수 가중치(RS/눌림/변동성) 재조정 근거로 사용")
    return 0


def run(top=10):
    universe, src = _load_universe()      # 2단 스크리닝: 동적(S&P500 딥스캔) 우선
    print(f"유니버스 스크리닝 ({len(universe)}종목 · {src}) — 안정 지향 스윙 후보\n")
    held = set()                                   # 이미 보유(레거시+실계좌) 제외(rank15)
    try:
        held = {r.get("symbol") for r in S.CLIENT.get_holdings().get("holdings", [])}
    except Exception as _he:
        print(f"⚠️ 보유 조회 실패({str(_he)[:50]}) — 보유종목이 매수후보로 보일 수 있음(감사#223)")
    held |= LEGACY
    rows, fails = [], []
    for sym in universe:                           # 종목별 예외 격리(rank13): 하나 터져도 전체 안 죽음
        try:
            r = analyze(sym)
        except Exception as e:
            fails.append(f"{sym}({type(e).__name__})"); continue
        if r:
            r["held"] = r["sym"] in held
            rows.append(r)
        else:
            fails.append(sym)
    coverage = len(rows) / len(universe) if universe else 0     # 커버리지 하한(rank14)
    if coverage < 0.70:
        print(f"⛔ 커버리지 저하 {coverage*100:.0f}% (<70%, 조회성공 {len(rows)}/{len(universe)}) "
              "— 표본 편향으로 신뢰 불가, 매매 금지. 네트워크 확인 후 재실행.")
        sys.exit(3)
    ranked = [r for r in rows if r.get("up")]      # 추세깨짐(150일선 아래)은 후보표서 제외(SC20)
    ranked.sort(key=lambda r: -r["score"])
    buy = [r for r in ranked if r["zone"] == "🟢매수존" and not r["held"]]
    edays = _load_earnings_days()                  # 실적일 통합 표시(D1)
    print(f"{'순위':<4}{'종목':<7}{'상태':<10}{'현재가':>10}{'RSI':>6}{'ATR%':>7}{'50선대비':>9}{'6M':>8}{'실적':>6}{'점수':>7}")
    print("-" * 80)
    for i, r in enumerate(ranked[:top], 1):
        r6 = "" if r["ret6m"] is None else f"{r['ret6m']:+.0f}%"
        tag = " 📌보유" if r.get("held") else ""
        ed = edays.get(r["sym"])
        eds = f"{ed}d" if ed is not None else "-"
        print(f"{i:<4}{r['sym']:<7}{r['zone']:<9}{r['last']:>10.2f}{r['rsi']:>6.0f}"
              f"{r['atr']:>6.1f}%{r['ext50']:>+8.1f}%{r6:>8}{eds:>6}{r['score']:>7}{tag}")
    print("-" * 80)
    print(f"\n🟢 지금 '매수존'(상승추세+눌림+비과열, 미보유): "
          f"{', '.join(r['sym'] + '(' + SECTORS.get(r['sym'], '기타')[:2] + ')' for r in buy) or '없음(관망)'}")
    _diff_and_snapshot(buy, ranked[:top])          # 사이클 diff + 스냅샷(D10/D8)
    print(f"(커버리지 {coverage*100:.0f}% — {len(rows)}/{len(universe)} 조회성공)")
    if fails:
        print(f"조회실패/제외: {', '.join(str(f) for f in fails)}")
    print("\n※ 매수 전 '실적일 임박' 확인(execute.py가 실적 2일내 진입 자동차단·1일전 자동청산).")
    print("※ 시세 기반 스크리너 — 매일 후보가 바뀜(고정 아님). 이미보유(📌)는 매수존서 제외.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--review":
        sys.exit(review(int(sys.argv[2]) if len(sys.argv) > 2 else 3))
    if len(sys.argv) > 1 and sys.argv[1] == "--daily-scan":
        sys.exit(daily_scan(int(sys.argv[2]) if len(sys.argv) > 2 else 80))
    run(int(sys.argv[1]) if len(sys.argv) > 1 else 10)
