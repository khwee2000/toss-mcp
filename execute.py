"""execute.py — 인덱스/우량주 스윙 전략 실행기 (검증된 핵심 주문경로만 사용).

급등주 executor(live_trade.py, LIVE_GO로 잠금·보류)와 별개. 여기선 잘 검증된
plan_order→place_order_confirmed(order-safety 29 tests, TOCTOU/드리프트/한도 게이트)
만 얇게 감싼다. 세션 가드·저널링 포함. 전략 미검증이므로 소액·신중.

CLI:
  --buy SYM USD     정규장에서 $USD 시장가 매수 (예: --buy GOOGL 35)
  --sell SYM QTY    시장가 매도 (전량/부분)
  --holdings        보유 조회
"""
from __future__ import annotations
import fcntl, json, os, sys, time
from datetime import datetime
from pathlib import Path

import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S        # noqa: E402
import market_cycle as MC  # noqa: E402

JOURNAL = Path(os.path.expanduser("~/.toss-trader/index_journal.jsonl"))
LOCKFILE = Path(os.path.expanduser("~/.toss-trader/execute.lock"))


def _acquire_lock():
    """단일 실행 락(money-safety). 크론 겹침/동시 --manage로 인한 이중 주문 방지.
    락 획득 실패(다른 실행 진행중)면 None. 성공 시 파일핸들 반환(프로세스 종료까지 유지 필수)."""
    try:
        LOCKFILE.parent.mkdir(parents=True, exist_ok=True)
        f = open(LOCKFILE, "w")
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f
    except (BlockingIOError, OSError):
        return None


_CRITICAL_EVENTS = {"BUY_FILLED", "BUY_PARTIAL", "BUY_WORKING", "BUY_UNCERTAIN",
                    "SELL_FILLED", "SELL_PARTIAL", "SELL_WORKING"}


def _rotate(path, max_bytes=5_000_000):
    """추가전용 파일 로테이션(T13): max_bytes 초과 시 .1로 이동(기존 .1 교체).
    저널(원장)은 대상 아님 — 로그/경보/메트릭 등 소모성 파일 전용. 실패해도 조용히."""
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            bak = path.with_suffix(path.suffix + ".1")
            if bak.exists():
                bak.unlink()
            path.rename(bak)
    except Exception:
        pass


def _atomic_write(path, text):
    """원자적 파일 쓰기(G3): tmp에 쓰고 rename — 도중 크래시로 상태파일이 반쪽 저장되는 것 방지.
    상태파일(alert_day/last_run 등) 전용. 실패 시 False."""
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        _chmod600(path)
        return True
    except Exception:
        return False


def _chmod600(path):
    """데이터파일 소유자 전용 퍼미션(G9: 계좌·거래 데이터 world-readable 방지). 실패 조용히."""
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _backup_journal(keep=14):
    """장부(원장) 일일 백업(T14): 뮤테이팅 실행 전 .backups/journal-<ET일>.jsonl 최초 1회 복사
    + sha256 체크섬 파일(G10: 복구선 자체가 손상되면 무의미 → 무결성 검증 가능하게).
    keep일 초과분 삭제. 실패해도 조용히(백업이 매매를 막지 않음)."""
    try:
        if not JOURNAL.exists():
            return
        bdir = JOURNAL.parent / ".backups"
        bdir.mkdir(parents=True, exist_ok=True)
        dst = bdir / f"journal-{_trading_day()}.jsonl"
        if not dst.exists():
            import hashlib
            data = JOURNAL.read_bytes()
            dst.write_bytes(data)
            _chmod600(dst)
            (bdir / (dst.name + ".sha256")).write_text(
                hashlib.sha256(data).hexdigest(), encoding="utf-8")
        old = sorted(bdir.glob("journal-*.jsonl"))
        for f in old[:-keep]:
            f.unlink()
            sha = f.with_name(f.name + ".sha256")
            if sha.exists():
                sha.unlink()
    except Exception:
        pass


def _verify_backup_checksum():
    """최신 백업의 sha256 검증(G10). (ok, detail) — 백업 없으면 (True, '백업 없음')."""
    try:
        import hashlib
        bdir = JOURNAL.parent / ".backups"
        backs = sorted(bdir.glob("journal-*.jsonl")) if bdir.exists() else []
        if not backs:
            return True, "백업 없음(첫 뮤테이팅 실행 시 생성)"
        f = backs[-1]
        sha = f.with_name(f.name + ".sha256")
        if not sha.exists():
            return True, f"{f.name}: 체크섬 파일 없음(구백업)"
        ok = hashlib.sha256(f.read_bytes()).hexdigest() == sha.read_text().strip()
        return ok, f"{f.name}: {'OK' if ok else '❌ 불일치(백업 손상!)'}"
    except Exception as e:
        return True, f"검증 불가({str(e)[:40]})"


def _scrub(text):
    """로그/경보 민감정보 마스킹(K8): Bearer 토큰·appkey/secret류 값이 실수로 메시지에
    섞여도 저장 전 가림. 원장(_j)은 대상 아님(체결 데이터에 secret 없음)."""
    import re
    t = str(text)
    t = re.sub(r"(Bearer\s+)[A-Za-z0-9\-_\.]{8,}", r"\1***", t)
    t = re.sub(r"((?:appkey|appsecret|secret|token|apikey|authorization)[\"']?\s*[:=]\s*[\"']?)"
               r"[A-Za-z0-9\-_\.]{6,}", r"\1***", t, flags=re.I)
    return t


def _log(msg):
    """구조적 운영 로그 — execute.log에 'KST시각 메시지' append(B3/T17: 죽은 크론/실패 사후추적).
    저널(_j, 포지션원장)과 별개의 사람이 읽는 실행 이력. 실패해도 조용히(로깅이 매매를 막지 않음)."""
    try:
        p = JOURNAL.parent / "execute.log"
        _rotate(p)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now(MC.KST).isoformat(timespec='seconds')} {_scrub(msg)}\n")
    except Exception:
        pass


_ALERT_EVENTS = {"BUY_WORKING", "BUY_UNCERTAIN", "SELL_WORKING", "SELL_RETRY",
                 "PORTFOLIO_ALERT", "PORTFOLIO_TP", "GAP_THROUGH_STOP", "BUY_CIRCUIT",
                 "EXIT_UNCORROBORATED", "EXIT_FORCED_STALE_BOOK",
                 "EXIT_SIGNAL_OFFHOURS", "IDLE_CAPITAL", "CONN_DEAD"}


def _alert(kind, msg):
    """크리티컬 이벤트 out-of-band 알림(N4): 지속 alerts.jsonl + 운영로그 + opt-in macOS 알림.
    _j(포지션 원장)와 별개 채널 — 저널 기록이 실패해도 사람이 놓치지 않게. 실패해도 조용히(매매 안 막음).
    ⚠️ _j를 호출하지 않는다(재귀 방지). 데스크톱 알림은 TOSS_DESKTOP_ALERTS=1일 때만(크론/테스트 무영향)."""
    try:
        rec = {"ts": datetime.now(MC.KST).isoformat(timespec="seconds"),
               "kind": kind, "msg": _scrub(msg)[:200]}
        p = JOURNAL.parent / "alerts.jsonl"
        _rotate(p)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
    _log(f"ALERT {kind}: {str(msg)[:120]}")
    if os.environ.get("TOSS_DESKTOP_ALERTS") == "1":
        try:
            import subprocess
            safe = str(msg)[:120].replace('"', "'")
            subprocess.run(["osascript", "-e",
                            f'display notification "{safe}" with title "toss-trader {kind}"'],
                           timeout=3, capture_output=True)
        except Exception:
            pass


def _set_halt(reason=""):
    """매매 전면동결 센티널 생성 — 사유를 파일에 기록하고 경보(T7/K5).
    기존 8곳의 무언(無言) touch()를 일원화: 왜 멈췄는지 HALT 파일만 봐도 알 수 있게.
    실패해도 조용히(동결 시도 자체가 크래시를 내면 안 됨)."""
    try:
        p = Path.home() / ".toss-mcp" / "HALT"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{datetime.now(MC.KST).isoformat(timespec='seconds')} {reason}\n",
                     encoding="utf-8")
    except Exception:
        pass
    _alert("HALT", reason or "manual")


def _j(ev):
    """저널 기록. 실체결/포지션 관련(critical) 이벤트는 기록 실패 시 유실을 막기 위해
    fallback 파일 + stderr + HALT sentinel까지 동원(rank12: 체결됐는데 기록 유실 방지).
    alert-worthy 이벤트는 저장과 별개로 out-of-band 경보(N4, 기록 성패와 무관하게 먼저 발화)."""
    ev = {"ts": datetime.now(MC.KST).isoformat(timespec="seconds"), **ev}
    if ev.get("event") in _ALERT_EVENTS:      # 크리티컬 → 사람에게 알림(N4)
        _alert(ev.get("event"),
               {k: ev[k] for k in ("sym", "pl", "pnlPct", "pnlUsd", "reason", "dte", "qty") if k in ev})
    line = json.dumps(ev, ensure_ascii=False)
    try:
        JOURNAL.parent.mkdir(parents=True, exist_ok=True)
        with JOURNAL.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            if ev.get("event") in _CRITICAL_EVENTS:   # 체결기록 전원단절 내구성(A11: flush+fsync)
                try:
                    f.flush(); os.fsync(f.fileno())
                except OSError:
                    pass
        try:
            os.chmod(JOURNAL, 0o600)      # 계좌 원장·체결이력 world-readable 방지(MS16)
        except OSError:
            pass
        return
    except Exception as e:
        if ev.get("event") not in _CRITICAL_EVENTS:
            return
        # 실체결 기록 실패 → 유령 방지 위해 최대한 남기고 매매 동결
        sys.stderr.write(f"[execute._j CRITICAL 기록실패 {e}] {line}\n")
        fb_ok = False
        try:
            fb = JOURNAL.parent / "index_journal.fallback.jsonl"
            with fb.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            fb_ok = True
        except Exception:
            pass
        _set_halt(f"critical 저널기록 실패({ev.get('event')} {ev.get('sym')}) — fallback으로만 기록됨")
        if not fb_ok:      # main·fallback 둘 다 실패 = 어디에도 기록 못 함 → 무기록 매매 금지(SL11/MS12)
            sys.stderr.write("[execute._j] critical 이벤트 기록 완전 실패 — 하드종료\n")
            sys.exit(3)


# ── 실보유(레거시) 잠금 + 예산/중복/서킷 가드 ─────────────────────────
LEGACY = {"MBRX", "000660", "005930"}  # 실계좌 장기바그(하이닉스·MBRX) — 전략 무접촉. AAPL·TSLA는 먼지(<$1)라 제외(2026-08-28 사용자 지시: 없는셈, 신규매수 허용)
BUDGET_USD = 362.0     # 재시작 실확정 USD(2026-08-28: 50만원→$362.07 환전 완료). 레거시바그·먼지 제외한 순수 트레이딩 캐시
# 예산/저널 로직과 '독립된' 하드 백스톱(SL10/MS#11): 저널 계산이 버그나 조작으로 무력화돼도
# 최후 방어선. config KRW 한도(수동/MCP 공유)는 건드리지 않고 전략 실행기에만 적용.
HARD_MAX_ORDER_USD = 91.0    # 단일주문 하드상한(집중화: 최대슬롯 0.25×362=$90.5 수용). 안전검수 반영: 집중캡(90.5)보다 살짝 위로 유지해 저널독립 백스톱 성질 보존
HARD_MAX_DAILY_USD = 362.0   # 오늘 투입 총액 하드상한(전액 배치 허용 — 종목수는 MAX_ENTRIES_PER_DAY가 별도 제한)
MAX_POSITIONS = 5      # 동시 보유 상한(개편 2026-08-28: 9→5 집중화 — 미니슬롯 분산이 엣지를 노이즈로 희석시킨 지난 실수 교정)
MAX_POSITION_WEIGHT = 0.25   # 단일 종목 최대 비중 — 집중화로 0.20→0.25(고확신 ~$90 슬롯 허용, 팻핑거 가드와 정합)
MAX_ENTRIES_PER_DAY = 5      # 하루 진입 상한(증액 바스켓 구성 허용, 런어웨이는 HARD_MAX_DAILY가 차단)
MAX_LOSS_CLOSES_PER_DAY = 2  # 하루 손실청산 상한(초과 시 당일 매매중단)
COOLDOWN_DAYS = 2            # 손절 후 동일종목 재매수 금지 일수(리벤지매매 방지)
MAX_DAILY_LOSS_USD = 30.0   # 오늘 실현손실 상한(예산 비례)
STOP_PCT = -6.0        # 손절 기준(개편 2026-08-28: -8→-6 타이트닝 — 지난 NVDA류 큰손실 누수 차단, +5~7%목표 대비 R:R 개선). 고변동주는 -1.5×ATR로 자동 확대
GAP_THROUGH_MARGIN = 2.0   # 손절선을 이만큼(%) 관통하면 '갭관통'(정상체결은 스탑 근처, 관통=급락갭, RB14)
PRICE_BOOK_TOL = 3.0       # 청산 직전 현재가가 호가창 밖으로 이만큼(%) 벗어나면 유령체결로 간주(감사#233)
UNCORROBORATED_MAX_STREAK = 3   # 같은 종목이 이 횟수 연속 보류되면 fail-closed 해제(감사#235 critical):
                                # 유령체결은 단발이라 3회 연속 재현되지 않는다 — 3연속은 '호가 API가 죽었다'는
                                # 뜻이고, 그때까지 막으면 손절 경로가 영구 소멸한다(TIME이 STOP에 가려 도달 불가)
TP_RSI = 65.0          # 익절 기준(기대감 강할 때 매도)
EARN_BLOCK_DAYS = 5    # 실적 이 일수 이내면 신규 진입 금지(개편 2026-08-28: 2→5 — 보유창 실적버퍼. 지난 MAR가 D-6 진입 후 발표 전 강제청산(-1.5%)으로 손실난 누수 교정: 목표도달 런웨이 확보)
EARN_EXIT_DAYS = 1     # 실적 이 일수 이내면 보유분 발표 전 청산
EARN_UNKNOWN_GAP_PCT = 4.0   # 실적일 미상 + 당일 |변동|≥이 값 → 미상 실적이벤트 의심 차단(RB6/C10)
EARN_POST_BLOCK_DAYS = 1     # 실적 직후 이 일수 이내 신규진입 금지(C8: 발표 직후 IV크러시/갭 소화기)
EARN_STALE_DAYS = 7          # #3(개편): 실적일이 이 일수 넘게 과거면 stale(낡음)로 보고 '미상(None)' 취급 — 낡은 날짜를 '안전'으로 오판 방지
ENTRY_MIN_ATR_PCT = 2.0      # 신규진입 ATR 하한(감사#243 검토로 1.0→2.0 복원): 목표 2×ATR·손절 -8% 구조에서
                             # ATR<2%면 R:R<0.5(목표 +2~4% vs 손절 -8%)로 EV 잠식 — 수수료까지 먹혀 순이익 0에 수렴.
                             # ATR≥2%에서만 R:R≥0.5 확보. 스크리너가 저변동 선호(-atr×2.5)라 이 하한이 필수 가드.
# ── 변동성 적응 사이징·목표(감사#243): 달러가 아니라 리스크를 균등화, 목표는 ATR 비례 ──
PORTFOLIO_DAILY_RISK_PCT = 0.032   # 포트폴리오 목표 일일 ATR 리스크(예산 대비) — 슬롯에 분배
BASE_ATR_PCT = 3.0           # ATR 미상 시 기준 변동성(사이징·목표 폴백)
TARGET_ATR_MULT = 2.0        # 목표가 = 진입 × (1 + 이배수 × ATR). 저변동 +4%·고변동 +8% 식으로 도달가능
TRAIL_ARM_PCT = 6.0          # 트레일링 무장선 상한(C1) — ATR 정규화 후에도 이보다 늦게 무장하지 않음
TRAIL_ARM_ATR_MULT = 1.5     # 무장선 = ATR × 이 배수(감사#234). 고정 6%는 종목마다 뜻이 달랐다:
                             # 실측 NEE 3.5 ATR(사실상 도달불가) vs NVDA 1.7 ATR → 저변동주에서만 사문화
TRAIL_ARM_PCT = 6.0          # 트레일링 무장선 상한(C1) — ATR 정규화 후에도 이보다 늦게 무장하지 않음
TRAIL_ARM_ATR_MULT = 1.5     # 무장선 = ATR × 이 배수(감사#234). 고정 6%는 종목마다 뜻이 달랐다:
                             # 실측 NEE 3.5 ATR(사실상 도달불가) vs NVDA 1.7 ATR → 저변동주에서만 사문화
TRAIL_ARM_FLOOR = 2.0        # 무장선 하한 — 이보다 낮으면 노이즈에 무장해 잦은 청산(처닝)
TRAIL_KEEP = 0.5             # 트레일링 보전선: 최고 이익의 이 비율 아래로 반납하면 청산(C1)
PARTIAL_TP_PCT = 5.0         # 부분익절 트리거(C2: +5% 도달 시 절반 익절, 잔량 러너)
PARTIAL_TP_FRACTION = 0.5    # 부분익절 비율
# 목표가가 PTP 임계에 근접하면 전량청산(목표가)이 먼저 걸려 PTP가 사문화된다(감사#232 라이브 확인:
# 5종목 중 3종목 발동불가). '러너를 남길 여지'가 실제로 있을 때만 부분익절을 무장한다.
PARTIAL_TP_MIN_GAP = 3.0     # 목표가가 PTP보다 최소 이만큼(%p) 위여야 부분익절 발동
# ── 개편 2026-08-28: 목표 피니시라인·개장 갭다운 방어(더잘하기 ②③) ──
PORTFOLIO_TP_PCT = 5.0       # ② 포트폴리오 피니시라인: (재시작후 실현+미실현) ≥ 예산의 이 %면 이익 포지션 일괄 익절(목표 확정). 손실분은 각자 손절선에 위임
RESTART_TS = "2026-08-28"    # 재시작 기준일 — 이후 실현손익만 피니시라인에 합산(레거시/과거 런 제외)
GAP_DOWN_BLOCK_PCT = -3.0    # ③ 개장매수 갭다운 방어: 당일변동(직전종가 대비) 이 값 이하면 발사 보류(떨어지는 칼 회피). 다음 분 재시도, 큐 유지
# ── Tier-2 개편 2026-08-28: 이긴 것 끝까지·유휴자본 알림 ──
RUNNER_TARGET_MULT = 1.6     # #5 이긴 것 끝까지: +5% 부분익절(절반) 후 잔량(러너)의 목표가를 원래 상승폭×이 배수로 상향(러너에 상방 여유). 하락은 트레일링·브레이크이븐이 방어
IDLE_CAPITAL_MIN_USD = 30.0  # #6 유휴자본 경보 하한: 0포지션인데 배치가능 USD가 이 값 이상이면 '스캔 필요' 알림(당일 1회). 3주 휴면 재발 방지
MAX_HOLD_DAYS = 20     # '거래일' 기준(C4: 종전 캘린더 28일≈20거래일 근사를 정확한 주중 계산으로)
TIME_EXIT_MIN_PROGRESS = 3.0   # 시간청산 진전 하한(%)
BREAKEVEN_PCT = 3.0    # 최고 이만큼↑ 갔던 종목이 본전 이하로 오면 청산(개편 2026-08-28 #1: 4→3 — 이긴 게 지는 것으로 바뀌는 것 방지, 소폭 winner 조기 본전방어)
MAX_PER_SECTOR = 1     # #2(개편): 동일 섹터 최대 보유 수(하드캡). 5슬롯 집중에서 상관성 높은 종목 겹치기(July MS+C 금융 동시보유) 차단 — 분산 강제
TREND_BREAK_BUFFER = 0.02   # 150일선 이 비율↓ 결정적 하회에만 추세이탈 청산(노이즈 오탈 방지·MDLZ 학습)

EARNINGS_FILE = Path(os.path.expanduser("~/.toss-trader/earnings_dates.json"))
# 알려진 실적일(수동 시드, 파일로 덮어쓰기 가능). 모르는 종목은 None.
EARNINGS_SEED = {"GOOGL": "2026-07-22", "MSFT": "2026-07-29", "META": "2026-07-29",
                 "AAPL": "2026-07-30", "XOM": "2026-07-31", "MRK": "2026-08-04",
                 "LLY": "2026-08-05", "NVDA": "2026-08-26"}


_LOAD_WARNED = set()   # 설정파일 파싱실패 경고 1회 노출용(T11)


def _load_earnings():
    data = dict(EARNINGS_SEED)
    try:
        if EARNINGS_FILE.exists():
            data.update(json.loads(EARNINGS_FILE.read_text(encoding="utf-8")))
    except Exception as e:      # silent {} → 실적가드·실적청산 조용히 꺼짐 방지(T11)
        if "earnings" not in _LOAD_WARNED:
            _LOAD_WARNED.add("earnings")
            print(f"⚠️ earnings_dates.json 파싱 실패({str(e)[:60]}) — 시드값만 사용(실적가드 약화 주의)")
            _alert("CONFIG", f"earnings_dates.json parse fail: {str(e)[:80]}")
    return data


def _is_last_session_before_earn(earn_date, today):
    """오늘이 실적 전 '마지막 미국 거래세션'인지: 다음 거래일이 실적일 이상이면 True(RB8/C19).
    캘린더 dte만 쓰면 금요일(월요일 실적)=dte3이라 dte≤1 청산 누락 → 주말/휴일 건너뛴 판정.
    주말만 건너뜀(휴일은 근사 — 주말이 지배적 케이스). earn_date≤today면 이미 당일/경과 → False."""
    from datetime import timedelta
    if earn_date <= today:
        return False
    nd = today + timedelta(days=1)
    while nd.weekday() >= 5:            # 토(5)·일(6) 건너뛰기
        nd += timedelta(days=1)
    return nd >= earn_date


def _earn_last_session(sym):
    """오늘이 sym 실적 전 마지막 거래세션인지(RB8/C19). 실적 미상/파싱실패 시 False."""
    e = _load_earnings().get(sym.upper())
    if not e:
        return False
    try:
        from datetime import date
        y, m, d = map(int, str(e)[:10].split("-"))
        today = date.fromisoformat(_trading_day())
        return _is_last_session_before_earn(date(y, m, d), today)
    except Exception:
        return False


def _days_to_earnings(sym):
    """알려진 실적일까지 남은 달력일수. 모르면 None(경고: 미상 종목은 갭 위험)."""
    e = _load_earnings().get(sym.upper())
    if not e:
        return None
    try:
        from datetime import date
        y, m, d = map(int, str(e)[:10].split("-"))
        today = date.fromisoformat(_trading_day())    # ET 거래일 기준(CX11 off-by-one 방지)
        dd = (date(y, m, d) - today).days
        # #3(개편): 과거로 낡은 실적일은 '다음 실적 미상'이므로 None 취급(fail-closed). 낡은 날짜를
        # '실적 지난 지 오래=안전'으로 오해해 진짜 임박 실적에 진입하는 것 방지 — earnings 갱신 필요 신호.
        if dd < -EARN_STALE_DAYS:
            return None
        return dd
    except Exception:
        return None


def _today_kst():
    return datetime.now(MC.KST).strftime("%Y-%m-%d")


def _trading_day(ts=None):
    """미국 거래일(ET date) 문자열. KST 자정(≈ET 11시, 정규장 중간)에 KST date로 버킷하면
    장중 서킷이 리셋돼 진입/손절 상한이 무력화되는 버그(CX3) → ET 날짜로 버킷. ts 없으면 현재."""
    from datetime import timedelta
    try:
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
    except Exception:
        et = None
    if ts:
        try:
            dt = datetime.fromisoformat(str(ts))
        except Exception:
            return str(ts)[:10]
    else:
        dt = datetime.now(MC.KST)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MC.KST)
    if et:
        return dt.astimezone(et).strftime("%Y-%m-%d")
    return (dt - timedelta(hours=_kst_et_offset(dt.month))).strftime("%Y-%m-%d")   # 월기반 근사(G7)


def _kst_et_offset(month):
    """KST→ET 시차 근사(G7 zoneinfo 폴백): 3~11월 EDT=13h, 12~2월 EST=14h.
    DST 전환일 ±수일 오차는 허용(폴백 자체가 zoneinfo 부재 시 비상용)."""
    return 13 if 3 <= month <= 11 else 14


TARGETS_FILE = Path(os.path.expanduser("~/.toss-trader/targets.json"))
PENDING_BUYS_FILE = Path(os.path.expanduser("~/.toss-trader/pending_buys.json"))


def _load_pending_buys():
    """개장 즉시 매수 큐(감사#241): [{sym, usd}]. 개장 '전에' 검증·승인된 주문만 담기고,
    개장 순간 exec_open_buys가 1분 안에 buy()로 발사한다. 느린 검증과 빠른 체결의 분리."""
    try:
        if PENDING_BUYS_FILE.exists():
            data = json.loads(PENDING_BUYS_FILE.read_text(encoding="utf-8"))
            out = []
            for it in (data if isinstance(data, list) else []):
                try:
                    out.append({"sym": str(it["sym"]).upper(), "usd": round(float(it["usd"]), 2),
                                "day": str(it.get("day") or "")})   # L1: 무장일 보존
                except (TypeError, ValueError, KeyError):
                    continue
            return out
    except Exception as e:
        # L3(감사#242): 손상을 조용한 빈 큐로 삼키지 않는다 — 형제 로더(targets)와 동일하게 경보.
        _alert("CONFIG", f"pending_buys 손상 — 개장매수 큐 무시({str(e)[:60]})")
    return []


def _save_pending_buys(q):
    return _atomic_write(PENDING_BUYS_FILE,
                         json.dumps(q, ensure_ascii=False, indent=2) + "\n")


def _arm_buy(sym, usd=None):
    """매수 큐에 종목 등록(중복 심볼은 최신 금액으로 갱신). 반환 q, 실패 시 None.
    usd 생략 시 리스크 균등 사이징으로 자동 계산(감사#243).

    감사#242 B3(fail-closed): 실적일 미상 종목은 큐 등록 자체를 거부한다. 완전자동 선정은
    사람의 실적 웹검증이 없으므로, earnings_dates.json에 등록된(=웹검증 후 --set-earnings 한)
    종목만 무장 가능 — 그러면 실적 이벤트 한복판 자동매수 구멍이 arm 시점에 원천 차단된다.
    L1: 무장일(_trading_day)을 스탬프해 stale 항목이 다음날 임의 개장에 발사되는 것 방지."""
    sym = sym.upper()
    if sym in LEGACY:
        print(f"⛔ {sym} 레거시 — 개장매수 큐 금지"); return None
    if _days_to_earnings(sym) is None:
        print(f"⛔ {sym} 실적일 미상 — 웹검증 후 --set-earnings 등록 전엔 개장매수 큐 금지(감사#242)")
        return None
    if usd is None:                       # 자동 리스크 사이징(감사#243)
        usd = _position_size_usd(_atr_pct(sym))
        print(f"  [리스크사이징] {sym} ATR 기반 ${usd:.0f} (달러균등 아님)")
    q = [it for it in _load_pending_buys() if it["sym"] != sym]
    q.append({"sym": sym, "usd": round(float(usd), 2), "day": _trading_day()})
    _save_pending_buys(q)
    return q


def _disarm_buy(sym):
    sym = sym.upper()
    q = [it for it in _load_pending_buys() if it["sym"] != sym]
    _save_pending_buys(q)
    return q



def _load_targets():
    """종목별 목표가(익절 price). {SYM: target_price}. manage가 현재가≥목표가면 자동 익절.
    키별 파싱(CX10): 한 종목 값이 깨져도 나머지 목표가는 유지."""
    out = {}
    try:
        if TARGETS_FILE.exists():
            for k, v in json.loads(TARGETS_FILE.read_text(encoding="utf-8")).items():
                try:
                    out[str(k).upper()] = float(v)
                except (TypeError, ValueError):
                    continue
    except Exception as e:      # silent {} → 목표익절 출구 조용히 소실 방지(T11)
        if "targets" not in _LOAD_WARNED:
            _LOAD_WARNED.add("targets")
            print(f"⚠️ targets.json 파싱 실패({str(e)[:60]}) — 목표가 미적용(익절 출구 소실 주의)")
            _alert("CONFIG", f"targets.json parse fail: {str(e)[:80]}")
    return out


_J_CACHE = {"key": None, "out": None}   # (mtime_ns,size)×2 키 캐시(I1: 사이클당 수십회 파싱 절감)


def _read_journal():
    """메인 저널 + fallback 저널을 병합(MS9/CX8: critical 실패로 fallback에만 남은 체결이
    포지션/서킷에서 안 보이는 유령 방지). (ts,event,sym,orderId) 중복 제거 후 시간순.
    파일 (mtime_ns,size) 키 캐시(I1) — append 시 자연 무효화, 외부수정도 mtime로 감지."""
    def _sig(p):
        try:
            st = p.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None
    fb = JOURNAL.parent / "index_journal.fallback.jsonl"
    key = (str(JOURNAL), _sig(JOURNAL), _sig(fb))
    if _J_CACHE["key"] == key and _J_CACHE["out"] is not None:
        return list(_J_CACHE["out"])          # 얕은 복사(호출부 정렬 등 변형 격리)
    seen, out = set(), []
    for path in (JOURNAL, JOURNAL.parent / "index_journal.fallback.jsonl"):
        try:
            if not path.exists():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                dedup = (str(ev.get("ts")), ev.get("event"), ev.get("sym"),
                         str(ev.get("orderId")))     # 감사#223: key 섀도잉 → 캐시 사문화였음
                if dedup in seen:
                    continue
                seen.add(dedup); out.append(ev)
        except Exception:
            pass
    out.sort(key=lambda e: str(e.get("ts", "")))
    _J_CACHE["key"] = key; _J_CACHE["out"] = list(out)
    return out


def _today_deployed_usd():
    """오늘(ET) 진입에 투입된 USD 합 — 하드 일일백스톱용(SL10). BUY_FILLED/PARTIAL/WORKING/UNCERTAIN
    합산(working→filled 이중계산 있어도 백스톱은 '과대추정=더 일찍 차단'이라 안전). 매도는 무관."""
    td = _trading_day()
    tot = 0.0
    for ev in _read_journal():
        if ev.get("event") in ("BUY_FILLED", "BUY_PARTIAL", "BUY_WORKING", "BUY_UNCERTAIN"):
            if _trading_day(ev.get("ts")) == td:
                tot += float(ev.get("filledUsd") or ev.get("usd") or 0)
    return tot


def _open_symbols_and_deployed():
    """전략 보유(가드용): (occupied set, deployed_usd). 확정체결 순qty>0 또는 미확정
    (working/uncertain) 슬롯 점유. 부분매도는 순qty·순usd 비례 반영(E3/CX2: 전체 슬롯
    해제로 물타기·예산 가드가 우회되던 버그 수정). 물타기·예산·종목수 가드 근거."""
    net = {}
    for ev in sorted(_read_journal(), key=lambda e: str(e.get("ts", ""))):
        e = ev.get("event"); sym = ev.get("sym")
        if not sym:
            continue
        # fusd(확정체결 usd)와 wusd(미확정 working usd)를 분리 → working→filled 이중계산 방지(A13)
        p = net.setdefault(sym, {"qty": 0.0, "fusd": 0.0, "wusd": 0.0, "working": False})
        if e in ("BUY_FILLED", "BUY_PARTIAL"):
            p["qty"] += float(ev.get("fillQty") or 0)
            p["fusd"] += float(ev.get("filledUsd") or ev.get("usd") or 0)
            p["working"] = False                 # 확정 체결로 승격
        elif e in ("BUY_WORKING", "BUY_UNCERTAIN"):
            p["working"] = True
            p["wusd"] = float(ev.get("filledUsd") or ev.get("usd") or 0)   # 최신 미확정 약정액
        elif e == "BUY_REJECTED":                # working 주문이 사후 종결(재폴링 T4) → 슬롯 해제
            p["working"] = False
            p["wusd"] = 0.0
        elif e in ("SELL_FILLED", "SELL_PARTIAL"):
            fq = float(ev.get("fillQty") or 0)
            if p["qty"] > 0 and fq > 0:
                frac = min(1.0, fq / p["qty"])
                p["fusd"] *= (1 - frac)
                p["qty"] = max(0.0, p["qty"] - fq)
            else:
                p["working"] = False             # working이었는데 매도 처리됨
    occ = {s for s, p in net.items() if p["qty"] > 1e-6 or p["working"]}
    # 배치액: 확정체결(qty>0)은 fusd, 아직 working만이면 wusd (둘 다 더하지 않음)
    dep = round(sum((net[s]["fusd"] if net[s]["qty"] > 1e-6 else net[s]["wusd"]) for s in occ), 2)
    return occ, dep


def _strategy_positions():
    """저널로 전략 '확정체결' 순보유 재구성. sym -> {qty, entryPx(수량가중), cost_usd, entryTs}.
    BUY_FILLED 누적 / SELL_FILLED 비례 차감. manage()·report()·서킷 근거(레거시와 무관, 전략분만)."""
    pos = {}
    for ev in sorted(_read_journal(), key=lambda e: str(e.get("ts", ""))):
        e = ev.get("event"); sym = ev.get("sym")
        if not sym:
            continue
        if e in ("BUY_FILLED", "BUY_PARTIAL"):
            fq = float(ev.get("fillQty") or 0); fp = float(ev.get("fillPrice") or 0)
            if fq <= 0:
                continue
            p = pos.setdefault(sym, {"qty": 0.0, "cost_usd": 0.0, "entryTs": ev.get("ts")})
            if p["qty"] <= 1e-6:      # 신규/재진입 → entryTs 갱신(감사#227: 가시성 임계와 일치
                p["entryTs"] = ev.get("ts")   # 고정 시 시간청산·트레일링·MFE가 오발동)
            p["qty"] += fq
            # 원가 계상(감사#225 치명): 체결가 미상 부분체결에 '주문 전액(usd)'을 쓰면 진입가가
            # 배수로 부풀어 즉시 가짜 손절이 난다. 우선순위: 실체결금액 → 수량비례 안분 → 0(과대금지).
            if fp:
                p["cost_usd"] += fq * fp
            elif ev.get("filledUsd") is not None:
                p["cost_usd"] += float(ev.get("filledUsd") or 0)
            elif e == "BUY_FILLED" and not ev.get("cum"):
                # 구형 전량체결 이벤트(증분 개념 이전)만 주문액 사용 — 그때는 usd≈실체결액
                p["cost_usd"] += float(ev.get("usd") or 0)
            # 그 외(체결가 미상 부분체결): 아무것도 더하지 않는다.
            # 주문 전액(usd)을 쓰면 진입가가 배수로 부풀어 즉시 '가짜 손절'이 난다(감사#227 critical).
            # 과소계상 → entryPx 낮음/0 → P/L 낙관 → 청산 미발동(보수적). 과대는 절대 금지.
        elif e in ("SELL_FILLED", "SELL_PARTIAL"):
            fq = float(ev.get("fillQty") or 0)
            if sym in pos and pos[sym]["qty"] > 0 and fq > 0:
                frac = min(1.0, fq / pos[sym]["qty"])
                pos[sym]["cost_usd"] *= (1 - frac)
                pos[sym]["qty"] = max(0.0, pos[sym]["qty"] - fq)   # 음수 방지(CX17 oversell)
    out = {}
    for s, p in pos.items():
        if p["qty"] > 1e-6:
            p["entryPx"] = (p["cost_usd"] / p["qty"]) if p["qty"] else None
            out[s] = p
    return out


def _rsi(sym, candles=None):
    """완료봉 RSI14. candles 주면 그걸 사용(SL7: manage 1회 fetch 공유), 없으면 자체 조회."""
    try:
        import indicators as IND
        c = candles if candles is not None else sorted(
            S.CLIENT.get_candles(sym, "1d", 60).get("candles", []), key=lambda r: str(r["timestamp"]))
        c_done = c[:-1] if len(c) > 15 else c    # 진행중 부분봉 제외(CX16: 스크리너/진입과 일관·재현성)
        return IND.compute(c_done).get("rsi14")
    except Exception:
        return None


def _fetch_daily(sym, target=200, max_pages=3):
    """일봉을 target개 이상 확보 — 첫 호출이 부족하면 before 커서로 과거 페이지 추가(RB11/D8).
    ⚠️ 토스 캔들 API는 count≤200만 허용(>200은 400) → 페이지당 count는 200으로 캡(exec#128 회귀수정).
    API가 요청보다 적게 줘도 장기지표(MA150=150봉)가 조용히 결측되지 않도록 보장.
    ts 오름차순 정렬·중복제거해 반환. 실패/무데이터면 빈 리스트."""
    out = {}
    before = None
    per_page = min(200, max(1, int(target)))     # API 상한(200) 준수
    for _ in range(max_pages):
        try:
            batch = S.CLIENT.get_candles(sym, "1d", per_page, before=before).get("candles", [])
        except Exception:
            break
        if not batch:
            break
        batch = sorted(batch, key=lambda r: str(r.get("timestamp")))
        before_ts = batch[0].get("timestamp")     # 가장 오래된 봉 → 다음 페이지 커서
        for r in batch:
            out[str(r.get("timestamp"))] = r
        if len(out) >= target or before_ts is None:
            break
        before = before_ts
    return [out[k] for k in sorted(out)]


def _ma150(sym, candles=None):
    """완료봉 150일 이동평균(추세이탈 청산용 S5). candles 주면 공유(SL7), 없으면 ≥252봉 조회(RB11).
    데이터 부족·실패 시 None."""
    try:
        c = candles if candles is not None else _fetch_daily(sym, 200)
        closes = [float(x["close"]) for x in c[:-1]]
        return sum(closes[-150:]) / 150 if len(closes) >= 150 else None
    except Exception:
        return None


def _atr_pct(sym, candles=None):
    """완료봉 ATR%(변동성 반영 손절용 C12). candles 주면 공유(SL7). 실패 시 None."""
    try:
        import trading_rules as TR
        c = candles if candles is not None else sorted(
            S.CLIENT.get_candles(sym, "1d", 40).get("candles", []), key=lambda r: str(r["timestamp"]))
        return TR.atr_pct(c[:-1], 14)
    except Exception:
        return None


def _mfe_pct(sym, entry, entry_ts, candles=None):
    """진입 이후 최고 상승폭(%, Max Favorable Excursion). 브레이크이븐용(C4).
    완료봉 '종가' 기준(exec#6: wick/노이즈 대신 — MDLZ 학습과 동일 원리)·ET 진입일 비교(exec#4).
    candles 주면 공유(SL7). 데이터 부족·실패 시 None."""
    if not entry or not entry_ts:
        return None
    try:
        c = candles if candles is not None else sorted(
            S.CLIENT.get_candles(sym, "1d", 40).get("candles", []), key=lambda r: str(r["timestamp"]))
        ed = _trading_day(entry_ts)               # ET 진입일
        c_done = c[:-1] if len(c) > 1 else c       # 진행중 부분봉 제외
        closes = [float(x["close"]) for x in c_done if str(x.get("timestamp"))[:10] >= ed]
        return (max(closes) / entry - 1) * 100 if closes else None
    except Exception:
        return None


def _days_held(entry_ts):
    """진입 이후 경과 일수(시간청산 S4). 파싱 실패 시 None."""
    if not entry_ts:
        return None
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(str(entry_ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MC.KST)
        return (datetime.now(timezone.utc) - dt).days
    except Exception:
        return None


def _trading_days_held(entry_ts):
    """진입 이후 경과 '거래일'(주중) 수 — 시간청산 기준(C4). 주말 제외한 정확한 카운트로
    캘린더 28일≈20거래일 근사를 대체(연휴 낀 포지션이 이르게/늦게 청산되던 오차 제거).
    미국 휴일은 근사상 포함(드묾). 파싱 실패 시 None."""
    if not entry_ts:
        return None
    try:
        from datetime import date, timedelta
        d0 = date.fromisoformat(_trading_day(entry_ts))
        d1 = date.fromisoformat(_trading_day())
        if d1 <= d0:
            return 0
        n, d = 0, d0
        while d < d1:
            d += timedelta(days=1)
            if d.weekday() < 5:
                n += 1
        return n
    except Exception:
        return None


def _exit_decision(pl, rsi, dte, cur, tgt, days_held=None, ma150=None, atr_pct=None, mfe_pct=None,
                   earn_last_session=False):
    """청산 사유 결정(우선순위: 실적임박 > 손절 > 추세이탈 > 브레이크이븐 > 목표가 > RSI > 시간청산).
    None이면 보유. manage()에서 분리해 단위테스트 가능(T7). 선택 인자는 구버전 호환.
    손절은 변동성 반영(C12): -max(8, 1.5*ATR%). 브레이크이븐(C4): +BREAKEVEN_PCT 갔던 종목이 본전 이하.
    earn_last_session(RB8): 주말/휴일 넘어가는 실적 전 마지막 세션이면 dte>1이어도 청산 보장."""
    stop = STOP_PCT if not atr_pct else -max(-STOP_PCT, 1.5 * atr_pct)
    if dte is not None and (0 <= dte <= EARN_EXIT_DAYS or (earn_last_session and dte >= 0)):
        tag = "" if 0 <= dte <= EARN_EXIT_DAYS else "·마지막세션"
        return f"실적 {dte}일전 → 발표 전 청산(기대감 실현{tag})"
    if pl is not None and pl <= stop:
        if pl <= stop - GAP_THROUGH_MARGIN:      # 스탑을 마진 이상 관통한 급락 갭 → 구분·경보(RB14/C8b/C14)
            return f"갭관통 손절 {pl:+.1f}%(스탑 {stop:.1f}% 관통 — 급락 갭, 즉시청산·경보)"
        widened = bool(atr_pct) and (1.5 * atr_pct > -STOP_PCT)   # 실제 넓어졌을 때만 '변동성' 표기(#21)
        return f"손절 {pl:+.1f}%(≤{stop:.1f}%{'·변동성' if widened else ''})"
    trend_break = (ma150 is not None and cur is not None and cur < ma150 * (1 - TREND_BREAK_BUFFER))
    # 이미 수익(pl>0)이고 목표가/RSI 도달이면 그게 진짜 청산사유 → 추세이탈 라벨보다 우선(SL6/exec#25).
    # 청산 여부는 동일(어차피 나감), 학습용 attribution(어떤 룰이 돈 버는지)만 정확화.
    profitable_tp = (pl is not None and pl > 0 and
                     ((tgt and cur is not None and cur >= tgt) or (rsi is not None and rsi >= TP_RSI)))
    if trend_break and not profitable_tp:
        return (f"추세이탈(150일선 {ma150:.2f} 대비 {(cur / ma150 - 1) * 100:.1f}% 하회, "
                f"버퍼 {TREND_BREAK_BUFFER * 100:.0f}%↓) → 청산(논지 무효)")
    if mfe_pct is not None and mfe_pct >= BREAKEVEN_PCT and pl is not None and pl <= 0:
        return f"브레이크이븐 청산(최고 +{mfe_pct:.1f}% 후 본전 {pl:+.1f}% 이탈 — 반납 방지)"
    # 트레일링 이익보전(C1): 크게 갔던(+TRAIL_ARM_PCT↑) 종목이 최고이익의 절반 밑으로 반납 → 청산.
    # 브레이크이븐(본전 방어)의 윗단계 — '기대감/강세에 판다' 철학의 코드화(완료봉 close MFE 기준).
    # 목표가/RSI 도달이면 그쪽이 진짜 청산사유 → 트레일링보다 라벨 우선(SL6와 동일 원칙, 감사#223)
    arm = _trail_arm_pct(atr_pct)      # 변동성 정규화 무장선(감사#234)
    if (mfe_pct is not None and mfe_pct >= arm and not profitable_tp
            and pl is not None and 0 < pl <= mfe_pct * TRAIL_KEEP):
        return (f"트레일링 청산(최고 +{mfe_pct:.1f}% → 현재 {pl:+.1f}%, "
                f"이익 {(1 - TRAIL_KEEP) * 100:.0f}%↑ 반납 — 남은 이익 보전, 무장선 {arm:.1f}%)")
    if tgt and cur is not None and cur >= tgt and (pl is None or pl > 0):
        # '익절'은 이익일 때만(감사#227): 낡거나 진입가보다 낮게 설정된 목표가가
        # 신규 포지션을 즉시 손실 청산시키던 갭. 손실 중이면 손절/추세 룰이 담당.
        return f"목표가 ${tgt:.2f} 도달 → 익절({(pl if pl is not None else 0):+.1f}%)"
    if rsi is not None and rsi >= TP_RSI and pl is not None and pl > 0:
        # C3: RSI 익절은 '이익일 때만' — 손실 중 RSI만으로 파는 비합리 제거(익절은 이익 실현)
        return f"익절 RSI {rsi:.0f}(≥{TP_RSI:.0f}, 기대감 강할 때 매도)"
    if (days_held is not None and days_held >= MAX_HOLD_DAYS
            and pl is not None and pl < TIME_EXIT_MIN_PROGRESS):
        return f"시간청산({days_held}거래일 보유·진전 {pl:+.1f}%<{TIME_EXIT_MIN_PROGRESS:.0f}% — 죽은자금)"
    return None


def _exit_category(reason):
    """청산 사유 문자열 → 카테고리(학습 분해용 T19). EARN/STOP/TARGET/RSI/OTHER 또는 None."""
    if not reason:
        return None
    if "실적" in reason:
        return "EARN"
    if "갭관통" in reason:      # 급락 갭 손절 — 정상 손절과 구분(RB14, "손절" 포함하므로 먼저 검사)
        return "GAPSTOP"
    if "손절" in reason:
        return "STOP"
    if "추세이탈" in reason:
        return "TREND"
    if "브레이크이븐" in reason:
        return "BREAKEVEN"
    if "트레일링" in reason:
        return "TRAIL"
    if "시간청산" in reason:
        return "TIME"
    if "목표가" in reason:
        return "TARGET"
    if "RSI" in reason:
        return "RSI"
    return "OTHER"


# 가격기반 청산만 호가 교차검증 대상. **EARN만** 순수 날짜 조건이다(_exit_decision의 실적
# 분기에 가격항이 없음). TIME은 이름과 달리 `days_held>=MAX_HOLD_DAYS AND pl<TIME_EXIT_MIN_PROGRESS`
# 로 현재가가 판정의 절반을 차지한다 — 감사#235에서 유령 저가체결이 스탑선 위(-8%~0%)에 찍히면
# STOP/TREND/BREAKEVEN/TRAIL을 전부 비껴가 TIME으로 빠져나가는 무게이트 경로가 실증됐다.
#
# 값은 유령체결이 어느 방향으로 그 룰을 발동시키는지 = 어느 호가와 대조할지:
#   "down" 유령 저가가 발동시킴 → 매수호가와 대조(그 가격에 실제로 팔리는가)
#   "up"   유령 고가가 발동시킴 → 매도호가와 대조
# 반대쪽 검사는 진양성이 구조적으로 0이고 오차단만 만든다(감사#235: 진짜 급락 중
# 체결가가 호가보다 위일 때 ask검사가 진짜 손절을 막던 것) — 그래서 방향별로 한쪽만 본다.
_PRICE_DRIVEN_EXITS = {"STOP": "down", "GAPSTOP": "down", "TREND": "down",
                       "BREAKEVEN": "down", "TRAIL": "down", "TIME": "down",
                       "TARGET": "up", "RSI": "up", "PTP": "up"}


def _offhours_alerted(sym):
    """오늘(ET) 이미 장외 청산신호 알림을 보낸 종목인지(감사#238 dedup).
    30분 사이클마다 같은 알림을 반복하지 않게 — 종목당 하루 1회."""
    td = _trading_day()
    for ev in _read_journal():
        if (ev.get("event") == "EXIT_SIGNAL_OFFHOURS" and ev.get("sym") == sym
                and _trading_day(ev.get("ts")) == td):
            return True
    return False


def _uncorroborated_streak(sym):
    """오늘(ET) 해당 종목의 연속 청산보류 횟수(감사#235). 실제 매도가 나가면 리셋.
    _sell_fail_streak과 동형 — 같은 성격의 실패(매도가 반복해서 안 나감)를 같은 방식으로 센다."""
    td = _trading_day()
    streak = 0
    for ev in _read_journal():
        if ev.get("sym") != sym or _trading_day(ev.get("ts")) != td:
            continue
        e = ev.get("event")
        if e == "EXIT_UNCORROBORATED":
            streak += 1
        elif e in ("SELL_FILLED", "SELL_PARTIAL"):
            streak = 0
    return streak


def _trail_arm_pct(atr_pct):
    """트레일링 무장선을 변동성으로 정규화(감사#234).

    고정 +6%는 종목마다 전혀 다른 사건을 뜻했다 — 실측 ATR 기준 NEE는 3.5배(사실상
    도달 불가), NVDA는 1.7배(흔한 움직임). 즉 저변동주에서만 트레일링이 구조적으로
    죽어 있었다. ATR 배수로 바꾸면 '이 종목 기준 의미 있는 상승'에서 일관되게 무장한다.

    상한은 종전 TRAIL_ARM_PCT — 어떤 종목도 예전보다 늦게 무장하지 않는다(보호는 넓어지기만).
    하한 TRAIL_ARM_FLOOR — 저변동주가 노이즈에 무장해 처닝하는 것 방지.
    ATR 미상이면 종전 고정값(보수적 폴백).
    """
    if not atr_pct or atr_pct <= 0:
        return TRAIL_ARM_PCT
    return max(TRAIL_ARM_FLOOR, min(TRAIL_ARM_PCT, TRAIL_ARM_ATR_MULT * atr_pct))


def _price_corroborated(sym, cur, side="down"):
    """청산 직전 현재가를 호가창으로 교차검증(감사#233, #235에서 정정). 반환 (ok, detail, mid).

    lastPrice는 '마지막 체결가'라 장외 유령체결 한 건이 그대로 남는다. 실측(1분봉):
    DE 2026-07-18 05:55 KST에 3주 거래로 473.49(-20.6%) 찍고 같은 분봉이 598로 마감,
    NVDA 2026-07-21 06:10에 189.54(-8.4%). 둘 다 진입가 대비 ±50% 위생검사(#225)를
    통과하므로 그대로 믿으면 가짜 손절이 나간다.

    side는 '유령체결이 어느 방향으로 이 룰을 발동시키는가'다. 반대쪽까지 검사하면
    진양성 0에 오차단만 생긴다 — 감사#235 실증: 진짜 급락이 진행돼 체결가가 호가보다
    위에 있을 때(548 vs 호가 528/529) ask검사가 진짜 -8% 손절을 막았다.
      · side="down": 매수호가가 현재가보다 TOL% 넘게 위 → 그 가격엔 안 팔린다 = 유령 저가
      · side="up"  : 현재가가 매도호가보다 TOL% 넘게 위 → 유령 고가(목표가 도달 오판)

    mid는 보류 시 포트폴리오 평가에 쓸 신뢰 가능한 가격. 유령가로 평가하면 가짜
    PORTFOLIO_ALERT가 당일 1회 래치를 소모해 진짜 하락 경보를 삼킨다(감사#235).
    호가를 아예 못 얻으면 mid=None → 호출부가 원가 중립 계상(시세실패 경로와 동일 규약).
    """
    bid = ask = None
    last_err = None
    for attempt in range(2):
        try:
            ob = S.CLIENT.get_orderbook(sym) or {}
            bids = ob.get("bids") or []
            asks = ob.get("asks") or []
            bid = float(bids[0]["price"]) if bids else None
            ask = float(asks[0]["price"]) if asks else None
        except Exception as e:
            last_err = e
            bid = ask = None
        if bid and ask and bid > 0 and ask > 0:
            break
        # 빈 호가도 재시도한다(감사#235): 개장 직후 미형성·정지 후 재개 직전 한쪽 공백 등
        # 일시적 상태가 예외보다 훨씬 흔한데, 종전 코드는 예외에만 재시도가 걸려 있었다.
        if attempt == 0:
            time.sleep(0.3)
    if not bid or not ask or bid <= 0 or ask <= 0:
        why = f"호가 조회 실패({type(last_err).__name__})" if last_err else "호가 비어있음"
        return False, why, None
    if bid > ask:      # 교차/역순 호가 = 피드 이상 → 가드가 조용히 fail-open 되지 않게
        return False, f"호가 역전(매수 {bid:.2f} > 매도 {ask:.2f}) — 피드 이상", None
    mid = (bid + ask) / 2
    if not cur or cur <= 0:
        return False, "현재가 비정상", mid
    if side == "down" and (bid / cur - 1) * 100 > PRICE_BOOK_TOL:
        return False, f"현재가 {cur:.2f}가 매수호가 {bid:.2f}보다 크게 아래(유령 저가체결 의심)", mid
    if side == "up" and (cur / ask - 1) * 100 > PRICE_BOOK_TOL:
        return False, f"현재가 {cur:.2f}가 매도호가 {ask:.2f}보다 크게 위(유령 고가체결 의심)", mid
    return True, f"호가 {bid:.2f}/{ask:.2f} 정합", mid


def _backtest_exit(bars, entry_px, entry_ts, tgt):
    """청산룰 what-if 시뮬레이터(N2, 순수·읽기전용): 진입 이후 완료 일봉을 하루씩 진행하며
    현재 _exit_decision을 적용해 '어느 거래일에 어떤 룰로 나갔을지'와 그때 손익을 계산.
    bars: 진입일 이후 완료 일봉(오름차순, OHLC). 반환 (day_idx, reason, exit_px, pl_pct);
    끝까지 미청산이면 (None, None, last_px, last_pl). 실적일 청산은 시뮬 제외(price-기반만)."""
    import indicators as IND
    import trading_rules as TR
    if not bars:
        return (None, None, entry_px, 0.0)
    for i in range(len(bars)):
        window = bars[:i + 1]
        closes = [float(x["close"]) for x in window]
        close = closes[-1]
        pl = (close / entry_px - 1) * 100
        rsi = IND.compute(window).get("rsi14") if len(window) >= 15 else None
        ma150 = sum(closes[-150:]) / 150 if len(closes) >= 150 else None
        atr = TR.atr_pct(window, 14) if len(window) >= 15 else None
        mfe = (max(closes) / entry_px - 1) * 100
        reason = _exit_decision(pl, rsi, None, close, tgt, ma150=ma150, atr_pct=atr, mfe_pct=mfe)
        if reason:
            return (i, reason, close, pl)
    last = float(bars[-1]["close"])
    return (None, None, last, (last / entry_px - 1) * 100)


def _realized_trades():
    """FIFO 매칭 실현거래. 각 {sym, qty, buyPx, sellPx, plPct, plUsd, sellTs}."""
    from collections import defaultdict, deque
    lots = defaultdict(deque); trades = []
    for ev in sorted(_read_journal(), key=lambda e: str(e.get("ts", ""))):
        e = ev.get("event"); sym = ev.get("sym")
        if not sym:
            continue
        if e in ("BUY_FILLED", "BUY_PARTIAL"):
            fq = float(ev.get("fillQty") or 0); fp = float(ev.get("fillPrice") or 0)
            if fq > 0 and fp <= 0:      # 체결가 미상 → '실체결금액'만 역산(T1/MH1: lot 유실 방지)
                # 주문 전액(usd)은 쓰지 않는다 — 부분체결이면 매수단가가 날조돼 실현손익·서킷·
                # 쿨다운이 오염된다(감사#227). 전량체결 구형 이벤트만 usd 허용.
                fu = float(ev.get("filledUsd") or 0)
                if fu <= 0 and e == "BUY_FILLED" and not ev.get("cum"):
                    fu = float(ev.get("usd") or 0)
                fp = fu / fq if fu > 0 else 0.0
            if fq > 0 and fp > 0:
                lots[sym].append([fq, fp])
        elif e in ("SELL_FILLED", "SELL_PARTIAL"):
            sq = float(ev.get("fillQty") or 0); sp = float(ev.get("fillPrice") or 0)
            if sq > 0 and sp <= 0:      # 매도가 미상 → filledUsd 역산(T1/MH1: P&L 누락·쿨다운/서킷 오염 방지)
                fu = float(ev.get("filledUsd") or 0)
                sp = fu / sq if fu > 0 else 0.0
            if sp <= 0:      # 그래도 미상 → lot 소비/기록 안 함(CX5: 오매칭·P&L오염 방지)
                continue
            while sq > 1e-9 and lots[sym]:
                lot = lots[sym][0]; take = min(sq, lot[0])
                if lot[1] > 0:
                    trades.append({"sym": sym, "qty": take, "buyPx": lot[1], "sellPx": sp,
                                   "plPct": (sp / lot[1] - 1) * 100, "plUsd": (sp - lot[1]) * take,
                                   "sellTs": ev.get("ts"), "exitCat": ev.get("exitCat")})
                lot[0] -= take; sq -= take
                if lot[0] <= 1e-9:
                    lots[sym].popleft()
    return trades


def _sell_fail_streak(sym):
    """오늘(ET) 해당 종목의 매도 실패 연속 횟수(A9). SELL_FILLED가 나오면 리셋.
    3회↑면 경보 대상(주문경로 문제 의심) — 단 손절 시도 자체를 막지는 않는다."""
    td = _trading_day()
    streak = 0
    for ev in _read_journal():
        if ev.get("sym") != sym or _trading_day(ev.get("ts")) != td:
            continue
        e = ev.get("event")
        if e in ("SELL_FAIL", "SELL_REJECTED", "SELL_RETRY"):
            streak += 1
        elif e in ("SELL_FILLED", "SELL_PARTIAL"):
            streak = 0
    return streak


DIVIDEND_FILE = Path(os.path.expanduser("~/.toss-trader/dividend_dates.json"))


def _days_to_dividend(sym):
    """배당락일까지 남은 일수(C9, 정보성). ~/.toss-trader/dividend_dates.json {SYM: YYYY-MM-DD}.
    NEE/XOM 같은 배당주의 배당락 전후 가격 왜곡(락일 하락=배당분)을 노이즈로 인지하기 위한 표시."""
    try:
        from datetime import date
        if not DIVIDEND_FILE.exists():
            return None
        d = json.loads(DIVIDEND_FILE.read_text(encoding="utf-8")).get(sym.upper())
        if not d:
            return None
        return (date.fromisoformat(str(d)[:10]) - date.fromisoformat(_trading_day())).days
    except Exception:
        return None


def _serenity_held_note(held_syms):
    """보유종목의 최근(48h) Serenity 언급 요약(E3, 캐시 전용 — manage에 네트워크 추가 안 함).
    serenity_check가 받아둔 캐시 파일만 읽어 [(sym, snippet)] 반환. 캐시 없거나 오래되면 []."""
    try:
        import re as _re
        from datetime import datetime as _dt, timedelta, timezone
        f = Path(os.path.expanduser("~/.toss-trader/serenity/aleabitoreddit_tweets.json"))
        if not f.exists():
            return []
        tw = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(tw, list):
            return []
        cut = _dt.now(timezone.utc) - timedelta(hours=48)
        out = []
        for t in tw:
            iso = str(t.get("createdAtISO") or "")
            try:
                if _dt.fromisoformat(iso) < cut:
                    continue
            except Exception:
                continue
            txt = t.get("text") or ""
            for s in held_syms:
                if _re.search(r"\$" + _re.escape(s) + r"\b", txt, _re.I):
                    out.append((s, txt.replace("\n", " ")[:100]))
                    break
        return out[:3]
    except Exception:
        return []


def _ptp_armed(entry, tgt):
    """부분익절 무장 여부(감사#232): 목표가가 PTP 임계보다 PARTIAL_TP_MIN_GAP(%p) 이상 위여야 한다.
    목표가 +5%·PTP +5%처럼 겹치면 전량청산(목표가)이 if/elif에서 먼저 걸려 PTP가 죽는다 —
    '절반 익절 후 러너 유지'라는 의도가 성립하려면 목표까지 실제 여유가 있어야 함.
    목표가 미설정이면 상한이 없으므로 무장(True)."""
    if not tgt or not entry:
        return True
    return (tgt / entry - 1) * 100 >= PARTIAL_TP_PCT + PARTIAL_TP_MIN_GAP


def _partial_tp_taken(sym):
    """현재 포지션에서 이미 부분익절(PTP)을 했는지(C2). '현재 포지션' = 마지막으로 수량이
    0이 된 시점 이후 — 과거 사이클의 PTP가 새 포지션에 이월되지 않게 저널 워크로 판정."""
    qty = 0.0
    taken = False
    for ev in sorted(_read_journal(), key=lambda e: str(e.get("ts", ""))):
        if ev.get("sym") != sym:
            continue
        e = ev.get("event")
        if e in ("BUY_FILLED", "BUY_PARTIAL"):
            qty += float(ev.get("fillQty") or 0)
        elif e in ("SELL_FILLED", "SELL_PARTIAL"):
            if ev.get("exitCat") == "PTP":
                taken = True
            qty = max(0.0, qty - float(ev.get("fillQty") or 0))
            if qty <= 1e-9:
                taken = False          # 포지션 종결 → PTP 이력 리셋(다음 포지션은 새로)
    return taken


def _strategy_circuit():
    """(ok, reason). 오늘 진입수/손실청산수 상한 초과 시 신규 진입 차단(rank19)."""
    today = _trading_day(); entries = 0        # ET 거래일 기준(KST 자정 리셋버그 방지 CX3)
    for ev in _read_journal():
        if _trading_day(ev.get("ts")) == today and ev.get("event") in (
                "BUY_FILLED", "BUY_PARTIAL", "BUY_WORKING", "BUY_UNCERTAIN"):
            entries += 1
    all_trades = _realized_trades()              # 1회 계산 재사용(I2: 저널 이중 파싱 제거)
    today_trades = [t for t in all_trades if _trading_day(t.get("sellTs")) == today]
    # 손실 '청산 횟수'는 매도 이벤트 단위로 센다(감사#227): FIFO 로트 매칭 행을 그대로 세면
    # 한 번의 매도가 여러 로트에 걸릴 때 1회 청산이 2~3회로 계산돼 당일 매매가 조기 중단된다.
    _loss_sells = {}
    for t in today_trades:
        k = (t.get("sym"), str(t.get("sellTs")))
        _loss_sells[k] = _loss_sells.get(k, 0.0) + t["plUsd"]
    losses = sum(1 for v in _loss_sells.values() if v < 0)
    realized_usd = sum(t["plUsd"] for t in today_trades)
    if entries >= MAX_ENTRIES_PER_DAY:
        return False, f"오늘 진입 {entries}회 — 상한 {MAX_ENTRIES_PER_DAY} 도달"
    if losses >= MAX_LOSS_CLOSES_PER_DAY:
        return False, f"오늘 손실청산 {losses}회 — 상한 {MAX_LOSS_CLOSES_PER_DAY}(당일 매매중단)"
    if realized_usd <= -MAX_DAILY_LOSS_USD:     # 실현손실 금액 서킷(C9 자본보호)
        return False, f"오늘 실현손실 ${realized_usd:.2f}(≤-${MAX_DAILY_LOSS_USD:.0f}) — 자본보호 중단"
    eq = peak = ddown = 0.0                      # 피크 대비 낙폭 서킷(C16/S10)
    for t in all_trades:
        eq += t["plUsd"]; peak = max(peak, eq); ddown = min(ddown, eq - peak)
    if ddown <= -(0.15 * BUDGET_USD):
        return False, f"실현 피크대비 낙폭 ${ddown:.2f}(≥15% 자본) — 신규중단(연속손실 보호)"
    return True, ""


COOLDOWN_MIN_LOSS_PCT = -3.0   # 이보다 얕은 손실(-0.9% 노이즈컷 등)은 쿨다운 미적용(C10/MDLZ 학습 연장)


def _in_cooldown(sym):
    """'유의미한' 손실 매도(≤-3%) 후 COOLDOWN_DAYS일 내 재매수 차단(S6 리벤지·휩쏘 방지).
    (blocked, reason). 익절·미세손실(추세이탈 노이즈컷 등)은 쿨다운 없음(C10) —
    좋은 셋업이 -1%짜리 컷 때문에 2일 잠기는 기회비용 제거. 진짜 손절만 식힌다."""
    from datetime import date
    today = datetime.now(MC.KST).date()
    sym = sym.upper()
    for t in _realized_trades():
        if t["sym"] == sym and t["plPct"] <= COOLDOWN_MIN_LOSS_PCT:
            ts = str(t.get("sellTs", ""))[:10]
            try:
                y, m, d = map(int, ts.split("-"))
                if 0 <= (today - date(y, m, d)).days < COOLDOWN_DAYS:
                    return True, f"손절 후 {COOLDOWN_DAYS}일 쿨다운(마지막 손실매도 {ts})"
            except Exception:
                continue
    return False, ""


def _reconcile_fill(oid, tries=6, wait=1.0):
    """place 후 실제 체결 확인(유령 포지션 방지). (status, fillQty, fillPrice).
    폴링 간격 지수백오프(A10): wait×2^i (1→2→4…s) — 레이트리밋/일시장애 상관실패 완화."""
    import time
    status, fq, fp = None, 0.0, None
    if not oid:
        return status, fq, fp
    for i in range(tries):
        try:
            o = S.CLIENT.get_order(order_id=oid) or {}
        except Exception:
            o = {}
        if not isinstance(o, dict):      # 비-dict 응답 방어(감사#227): 역참조 예외가 buy/sell을
            o = {}                       # 관통해 '주문은 나갔는데 저널 기록 없음'(유령)을 만들던 갭
        status = o.get("status") or status
        ex = o.get("execution") if isinstance(o.get("execution"), dict) else {}
        try:
            fq = float(ex.get("filledQuantity") or o.get("filledQuantity") or fq or 0)
        except Exception:
            pass
        ap = ex.get("averageFilledPrice") or o.get("averageFilledPrice")
        if ap not in (None, ""):
            try:
                _v = float(ap)
                if _v > 0:      # "0.00"/"0" 등은 체결가 미상 취급(CX6: fp=0 오염 방지)
                    fp = _v
            except Exception:
                pass
        if (fp is None or fp <= 0) and fq > 0:   # 평균가 미상 → 체결금액/수량 역산(T2/MH1b)
            fa = ex.get("filledAmount") or o.get("filledAmount")
            try:
                _a = float(fa or 0)
                if _a > 0:
                    fp = round(_a / fq, 6)
            except Exception:
                pass
        if status in ("FILLED", "CANCELED", "REJECTED", "REPLACED"):
            break
        if i < tries - 1:
            time.sleep(min(wait * (2 ** i), 8.0))   # 지수백오프, 상한 8s(A10)
    return status, fq, fp


def _recorded_fill_qty(oid, kind):
    """orderId별 이미 저널에 기록된 체결수량 합(kind: 'BUY'|'SELL'). 멱등기록의 기준(T5/MH7)."""
    if not oid:
        return 0.0
    tot = 0.0
    evs = (f"{kind}_FILLED", f"{kind}_PARTIAL")
    for ev in _read_journal():
        if ev.get("event") in evs and str(ev.get("orderId")) == str(oid):
            tot += float(ev.get("fillQty") or 0)
    return tot


def _record_fill(kind, sym, oid, status, fq, fp, extra=None):
    """체결 기록(멱등, T5/MH7): 같은 orderId로 이미 기록된 수량을 빼고 '증분'만 저널.
    재기록·DUPLICATE 채택·재폴링이 겹쳐도 이중계상 불가(stale-coid 이중롱 MH3 차단).
    PARTIAL_FILLED는 *_PARTIAL 이벤트로(T3/MH2), cum=브로커 누적체결. 반환=기록된 증분수량."""
    fq = float(fq or 0)
    if fq <= 0:
        return 0.0
    done = _recorded_fill_qty(oid, kind)
    delta = round(fq - done, 6)
    if delta <= 1e-9:
        return 0.0          # 이미 전부 기록됨 — 중복 기록 안 함
    evname = f"{kind}_PARTIAL" if status == "PARTIAL_FILLED" else f"{kind}_FILLED"
    ev = {"event": evname, "sym": sym, "orderId": oid, "status": status,
          "fillQty": delta, "fillPrice": fp, "cum": fq,
          "filledUsd": round(delta * fp, 2) if fp else None}
    if extra:
        for k, v in extra.items():
            ev.setdefault(k, v)
    _j(ev)
    return delta


MAX_SPREAD_PCT = 0.5   # 매수 시 호가 스프레드 상한(넓으면 슬리피지 위험 → 보류)


def _spread_ok(sym):
    """(ok, spread_pct or None). 호가 스프레드가 넓으면(>0.5%) 매수 보류(rank18 슬리피지 가드).
    매도(특히 손절)엔 적용 안 함 — 청산은 스프레드를 물더라도 나가는 게 우선."""
    try:
        ob = S.CLIENT.get_orderbook(sym)
        ba = float(ob.get("bestAsk") or 0); bb = float(ob.get("bestBid") or 0)
        if ba <= 0 or bb <= 0:
            return True, None    # 호가 불명 → 서버 드리프트/한도 게이트에 위임(과차단 방지)
        sp = (ba - bb) / ((ba + bb) / 2) * 100
        return (sp <= MAX_SPREAD_PCT), sp
    except Exception:
        return True, None


def _recent_trade(sym, max_age_s=300):
    """최근 실체결(≤max_age_s초)이 있으면 True — 실제 개장의 신뢰 신호.
    타임스탬프 견고화(MS11/CX12): Z/offset/epoch 처리, naive는 UTC 아닌 토스피드 tz(KST) 가정,
    '미래' 시각(age<0)은 recent로 인정 안 함(stale을 open으로 오판하는 위험 방향 차단)."""
    try:
        from datetime import timezone
        tr = S.CLIENT.get_trades(sym, 3)
        rows = tr.get("trades") or tr.get("data") or []
        ts = max((str(r.get("timestamp") or r.get("time") or "") for r in rows), default="")
        if not ts:
            return False
        ts2 = ts.replace("Z", "+00:00")
        if ts2.replace(".", "").isdigit():        # epoch(초 또는 ms)
            v = float(ts2); v = v / 1000.0 if v > 1e11 else v
            last = datetime.fromtimestamp(v, tz=timezone.utc)
        else:
            last = datetime.fromisoformat(ts2)
            if last.tzinfo is None:               # naive → 토스 피드 tz(KST) 가정
                last = last.replace(tzinfo=MC.KST)
        age = (datetime.now(timezone.utc) - last).total_seconds()
        # 시계오차로 체결ts가 로컬보다 수초 미래일 수 있음 → -120초 허용. 단 대형 미래값
        # (naive tz 오판 시 ~9h)은 여전히 배제해 stale을 open으로 오판하지 않음.
        return -120 <= age <= max_age_s
    except Exception:
        return False


def _market_open():
    """개장 여부. (True/False/None, 사유).
    ⚠️ 실측(2026-07-15): 토스 US market-calendar가 조회한 '오늘'을 상시 isHoliday=True로
    반환하는 오작동 → 앱은 주문 체결되는데 API만 MARKET_CLOSED로 막혔다(원인=우리 사전체크).
    실체결 최신성으로 백스톱: 캘린더가 '휴장'이라도 최근 체결이 있으면 실제 개장으로 교정.
    체결 없으면(진짜 휴장) 차단 유지(fail-safe)."""
    try:
        cal = S.CLIENT.get_market_calendar("US")
    except Exception:
        cal = None
    closed = (cal is None) or bool(cal.get("isHoliday")) or (cal.get("isOpen") is False)
    if not closed:
        return True, "개장(캘린더)"
    # 프록시 바스켓(MS12/CX13): 한 종목 halt/피드갭에도 다른 게 있으면 개장 판정
    if any(_recent_trade(s) for s in ("AAPL", "MSFT", "NVDA", "SPY")):
        return True, "개장(실체결 확인 — 캘린더 휴장은 오작동)"
    return False, f"휴장/미거래({(cal or {}).get('date', '')})"


# 섹터 맵(C7 분산 소프트가드용) — 스크리너 유니버스 중심, 없는 티커는 '기타'
SECTORS = {
    "AAPL": "테크", "MSFT": "테크", "GOOGL": "테크", "AMZN": "테크", "META": "테크",
    "NVDA": "반도체", "AVGO": "반도체", "TSM": "반도체", "AMD": "반도체", "MU": "반도체",
    "TXN": "반도체", "LRCX": "반도체", "AMAT": "반도체", "INTC": "반도체",
    "XOM": "에너지", "CVX": "에너지", "COP": "에너지",
    "NEE": "유틸", "DUK": "유틸", "SO": "유틸",
    "MRK": "헬스", "LLY": "헬스", "JNJ": "헬스", "UNH": "헬스", "BMY": "헬스",
    "VRTX": "헬스", "GILD": "헬스", "ELV": "헬스",
    "PG": "필수소비", "KO": "필수소비", "PEP": "필수소비", "CL": "필수소비",
    "MDLZ": "필수소비", "MO": "필수소비", "PM": "필수소비",
    "DE": "산업재", "CAT": "산업재", "GE": "산업재", "HON": "산업재", "RTX": "산업재",
    "WFC": "금융", "C": "금융", "JPM": "금융", "BAC": "금융", "MS": "금융", "GS": "금융",
    "AXP": "금융", "SCHW": "금융", "BLK": "금융", "SPGI": "금융", "V": "금융", "MA": "금융",
    "HD": "임의소비", "SBUX": "임의소비", "TGT": "임의소비", "BKNG": "임의소비",
}


try:      # S&P500 전수 섹터맵 병합(수동 SECTORS 우선 — 동적 유니버스 500종목 커버)
    import json as _json
    _full = _json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "sp500_sectors.json"), encoding="utf-8"))
    for _k, _v in _full.items():
        SECTORS.setdefault(_k, _v)
except Exception:
    pass


def _sector_overlap(sym, held_syms):
    """신규 진입 종목이 보유 종목과 같은 섹터인지(C7). (겹침 여부, 겹친 보유종목 리스트).
    소프트 가드 — 차단하지 않고 경고만(분산 판단은 재량, 억지 차단은 기회비용)."""
    sec = SECTORS.get(sym.upper())
    if not sec:
        return False, []
    dup = [h for h in held_syms if SECTORS.get(h) == sec]
    return bool(dup), dup


EXT_MAX_SPREAD_PCT = 0.35   # 장외 예외진입 스프레드 상한(정규장 0.5%보다 타이트, fail-closed)
EXT_MIN_DROP_PCT = -3.0     # 장외 예외진입 최소 디스로케이션(당일 -3%↓ 급락만 — '너무 심할 때')


def _ext_entry_ok(sess, spread_pct, day_change_pct):
    """장외(프리/애프터) 예외진입 허용 판정(사용자 위임 2026-07-20: '너무 심하면 들어가라').
    (ok, reason). 조건 전부 충족해야: ①프리/애프터 세션 ②호가 확인+타이트 스프레드(fail-closed —
    정규장과 달리 호가 미확인이면 거부) ③당일 -3%↓ 급락 디스로케이션. 평시 장외 진입은 계속 금지."""
    if sess not in ("미국 프리마켓", "미국 애프터마켓"):
        return False, f"세션 {sess} — 장외 예외는 프리/애프터마켓만"
    if spread_pct is None:
        return False, "호가 스프레드 미확인 — 장외는 fail-closed(얇은 호가 시장가 금지)"
    if spread_pct > EXT_MAX_SPREAD_PCT:
        return False, f"스프레드 {spread_pct:.2f}% > {EXT_MAX_SPREAD_PCT}% — 호가 얇음"
    if day_change_pct is None or day_change_pct > EXT_MIN_DROP_PCT:
        dcs = f"{day_change_pct:+.1f}%" if day_change_pct is not None else "미확인"
        return False, f"디스로케이션 부족(당일 {dcs}) — 급락({EXT_MIN_DROP_PCT:.0f}%↓)일 때만 장외진입"
    return True, f"장외 예외조건 충족(당일 {day_change_pct:+.1f}% 급락·스프레드 {spread_pct:.2f}%)"


def _concentration_ok(usd):
    """단일 종목 진입액이 예산의 MAX_POSITION_WEIGHT 이하인지 — 과집중 방지(재분석#129).
    한 종목(특히 고변동 NVDA류)이 포트폴리오 리스크를 지배(45%)하던 걸 막는다. True면 진입 허용."""
    return usd <= MAX_POSITION_WEIGHT * BUDGET_USD


def _position_size_usd(atr_pct):
    """리스크 균등 사이징(감사#243): 달러를 ATR에 반비례시켜 고변동주는 적게·저변동주는 많이 담는다.
    한 종목(GEN류)이 포트폴리오 리스크를 지배하던 불균형 해소.

    감사#244 정직화(🟡): divisor는 '고정 MAX_POSITIONS'라 이건 **슬롯별 리스크 상한**이지 포트폴리오
    전체 리스크를 강제하는 게 아니다 — 저변동 유니버스에선 달러캡(0.2×예산)에 먼저 물려 슬롯이 다
    안 차 총리스크가 목표에 미달할 수 있다(과소리스크=보수적, 안전방향). ATR 미상이면 기준(BASE).
    최소주문(10)·과집중상한 클램프. round 대신 floor(양수 int)로 절대 올림 안 함."""
    atr = atr_pct if (atr_pct and atr_pct > 0) else BASE_ATR_PCT
    risk_per_slot = BUDGET_USD * PORTFOLIO_DAILY_RISK_PCT / max(1, MAX_POSITIONS)
    usd = risk_per_slot / (atr / 100.0)
    hi = MAX_POSITION_WEIGHT * BUDGET_USD
    return float(int(max(40.0, min(hi, usd))))    # floor $40(집중화 2026-08-28: 미니슬롯 방지) — floor(보수적)·절대 올림 금지


def _atr_target(entry_px, atr_pct):
    """변동성 스케일 목표가(감사#243): 진입 × (1 + TARGET_ATR_MULT×ATR). 고정 +5% 대신 —
    저변동주는 작은(도달가능) 목표, 고변동주는 큰 목표. ATR 하한 배제 없이 전 유니버스 진입 가능.
    ATR 미상이면 기준값. 소수 2자리(라운드넘버 회피는 호출부에서 별도)."""
    atr = atr_pct if (atr_pct and atr_pct > 0) else BASE_ATR_PCT
    return round(entry_px * (1 + TARGET_ATR_MULT * atr / 100.0), 2)


def _earn_unknown_gap_block(dte, day_change_pct):
    """실적일 미상(None) + 당일 |변동|≥EARN_UNKNOWN_GAP_PCT → '알려지지 않은 실적 이벤트' 의심 차단(RB6/C10).
    실적일을 아는 종목은 EARN_BLOCK_DAYS 가드가 처리 → 여기선 None만 대상(fail-closed)."""
    return (dte is None and day_change_pct is not None
            and abs(day_change_pct) >= EARN_UNKNOWN_GAP_PCT)


def _guard(skip_market=False, allow_extended=False):
    if (Path.home() / ".toss-mcp" / "HALT").exists():
        print("🛑 HALT 파일 존재 — 매매 전면 중단"); return False
    if not S.CFG.allow_live_orders:
        print("⛔ 실주문 OFF"); return False
    if skip_market:      # 호출부(manage)가 이미 세션·개장 확인 → 중복 캘린더조회 생략(SL8/exec#8)
        return True
    sess = MC.us_session()
    if sess != "미국 정규장":
        if allow_extended and sess in ("미국 프리마켓", "미국 애프터마켓"):
            print(f"⚠️ 장외 세션({sess}) 예외진입 모드 — 급락조건+지정가+타이트스프레드 강제")
        else:
            print(f"⛔ 세션 {sess} — 정규장(22:30~05:00 KST)에만 매매"); return False
    mo, why = _market_open()               # 브로커 캘린더(휴장일 반영)
    if mo is False:
        print(f"⛔ 브로커 캘린더: {why} — 매매 불가(오늘 휴장)"); return False
    return True


MAX_DAY_POP = 2.5   # 프리장/당일 이미 이만큼↑ 오르면 추격 금지


def _day_change(sym):
    """당일(프리장 포함) 상승률 = 현재가 vs 직전 완료 거래일 종가.
    토스 일봉의 최신봉은 '현재 진행중 세션'이라 그 종가가 실시간가와 같다 →
    기준 종가는 항상 그 직전 봉(dc[-2]). 벽시계(KST) 날짜 비교는 ET와 어긋나
    자정(KST) 이후 추격 게이트가 조용히 꺼지는 버그가 있어 쓰지 않는다(감사 rank9)."""
    try:
        cur = float(S.CLIENT.get_price(sym)["lastPrice"])
        dc = sorted(S.CLIENT.get_candles(sym, "1d", 6).get("candles", []),
                    key=lambda r: str(r["timestamp"]))
        if len(dc) < 2:
            return None, None
        # 진행중봉 판정(감사#230): 토스는 최신봉을 KST 날짜로 스탬프해서 ET 거래일과 어긋난다
        # (예: ET 7/20 장중/직후에 최신봉이 '2026-07-21'로 라벨). ET날짜만 비교하면 최신봉을
        # 완료봉으로 오인해 기준가=현재가가 되고 당일변동이 전 종목 0%로 죽는다(추격가드·장외
        # 급락감지 무력화). → '날짜가 ET거래일 이상'이거나 '종가≈현재가(라이브 추종)'면 진행중.
        newest = float(dc[-1]["close"])
        newest_day = str(dc[-1].get("timestamp") or "")[:10]
        tracks_live = bool(cur) and abs(newest - cur) / cur < 0.001
        by_date = bool(newest_day) and newest_day >= _trading_day()
        pc = float(dc[-2]["close"]) if (tracks_live or by_date) else newest
        return (cur / pc - 1) * 100, cur
    except Exception:
        return None, None


def intraday_entry_check(sym):
    """진입 직전 프리장/당일 상승폭 + 1분봉 확인. 이미 급등했으면 추격 금지,
    최근 3분 급등도 차단, 눌림/안정 구간만 진입. (ok, note) 반환. fail-closed."""
    day, _ = _day_change(sym)
    if day is None:
        return False, "당일 상승률 조회 실패 — 안전대기(fail-closed, 감사 rank8)"
    if day > MAX_DAY_POP:
        return False, f"프리장/당일 {day:+.1f}% 이미 급등 → 추격 금지(반납 위험)"
    try:
        raw = S.CLIENT.get_candles(sym, "1m", 30).get("candles", [])
        rows = sorted(raw, key=lambda r: str(r.get("timestamp")))[-12:]
        if len(rows) < 5:      # 1분봉 부족(개장직후/희소) → fail-closed 대기(exec#15)
            return False, f"1분봉 {len(rows)}개(<5) 부족 — 안전대기(당일 {day:+.1f}%)"
        # 신선도 검증(감사#223): 전일 정규장 봉으로 오늘 모멘텀을 판정하던 갭 — 30분 초과 시 대기
        try:
            from datetime import timezone as _tz
            lastb = datetime.fromisoformat(str(rows[-1].get("timestamp")).replace("Z", "+00:00"))
            if lastb.tzinfo is None:
                lastb = lastb.replace(tzinfo=MC.KST)
            age_m = (datetime.now(_tz.utc) - lastb).total_seconds() / 60
            if age_m > 30:
                return False, f"1분봉 {age_m:.0f}분 전 데이터(장외/지연) — 안전대기(fail-closed)"
        except Exception:
            return False, "1분봉 시각 파싱 실패 — 안전대기(fail-closed)"
        closes = [float(r["close"]) for r in rows]
        cur = closes[-1]                       # 현재가(진행중 분봉)
        done = closes[:-1]                     # 완료 분봉만(CX21: 진행중 분 제외 → 안정적 모멘텀)
        m3 = (done[-1] / done[-4] - 1) * 100 if len(done) >= 4 else 0.0
        vwap = None
        try:
            vwap = (S.t_intraday_vwap(sym).get("data") or {}).get("vwap")
        except Exception:
            pass
        if m3 > 1.5:
            return False, f"최근 3분 {m3:+.1f}% 급등 → 추격 금지, 눌림 대기"
        if m3 < -2.0:
            return False, f"최근 3분 {m3:+.1f}% 급락 → 진정 대기"
        note = f"당일 {day:+.1f}%" if day is not None else ""
        note += f", 3분 {m3:+.1f}%"
        if vwap:
            note += f", VWAP {(cur/float(vwap)-1)*100:+.1f}%"
        return True, f"양호({note.lstrip(', ')})"
    except Exception:
        return False, "1분봉 데이터 실패 — 안전대기(다음 사이클, fail-closed)"


def _regime_risk_off():
    """시장 레짐(S7): QQQ가 200일선 아래면 risk_off(True) → 신규 진입 억제(하락장서 눌림매수 회피).
    index_engine의 검증된 200MA 페이지네이션 재사용. 조회실패/판단불가는 False(진입 허용, fail-open)."""
    try:
        import index_engine as IE
        t = IE.trend("QQQ")
        return bool(t.get("ok")) and (t.get("above") is False)
    except Exception:
        return False


def _entry_guard(sym):
    """진입 직전 논지 독립 재검증(S14 과열회피 + S15 방어심층). (ok, reason). fail-closed.
    크론이 넘긴 심볼을 buy()가 다시 스크리너 지표로 확인 — 상승추세·매수존(RSI38~58)·비과열.
    과열 기대감주(RSI 높고 50선 크게 위)는 '사는 자리 아님'(사용자 규칙)."""
    try:
        import stock_screener as SC
        r = SC.analyze(sym)
    except Exception:
        return False, "진입 재검증 조회 실패 — 보류(fail-closed)"
    if not r:
        return False, "지표 부족 — 보류"
    if r.get("zone") != "🟢매수존":     # zone이 추세·RSI·과열·급락(knife)·갭·실적임박을 모두 포함(#14)
        return False, (f"매수존 아님({r.get('zone')} RSI {r.get('rsi')}·50선 "
                       f"{r.get('ext50', 0):+.0f}%) — 진입 금지")
    atr = r.get("atr")
    if atr is not None and atr < ENTRY_MIN_ATR_PCT:
        # 감사#240: 목표 +5%가 통계적으로 도달 가능하려면 ≤~2.4 ATR이어야 한다.
        # 실측 — 승리 3건(GOOGL/MRK/XOM)은 목표가 1.3~2.4 ATR, NEE(3.0 ATR)만 죽은 돈.
        # 저변동주는 이 스윙 시스템의 목표 구조와 안 맞는다(변동성 정규화, #234와 동일 원리).
        return False, (f"ATR {atr:.2f}% < {ENTRY_MIN_ATR_PCT}% — 저변동주는 +5% 목표가 "
                       f"{5.0/atr:.1f}ATR(>2.4)라 도달 느림, 진입 제외")
    return True, f"논지 OK(매수존 RSI {r.get('rsi')}·ATR {atr}%)"


def buy(sym, usd, ext=False):
    """ext=True(--buy-ext): 장외 예외진입 — _ext_entry_ok(급락+타이트스프레드) 충족 시
    '지정가(현재가+0.1% 캡)'로만 주문(사용자 위임: 극단상황 재량, 평시는 정규장 온리)."""
    if not _guard(allow_extended=ext):
        return 1
    sym = sym.upper()
    if sym in LEGACY:
        print(f"⛔ {sym} 은 실보유(레거시) 종목 — 전략이 절대 매매 안 함"); return 1
    usd = round(float(usd), 2)
    if usd < 10 or usd > HARD_MAX_ORDER_USD:
        print(f"⛔ 금액 ${usd} — $10~{HARD_MAX_ORDER_USD:.0f} 범위(집중화: 상한=단일주문 하드상한)"); return 1
    if usd > HARD_MAX_ORDER_USD:             # 예산로직과 독립된 하드 상한(SL10 백스톱)
        print(f"⛔ 단일주문 ${usd} > 하드상한 ${HARD_MAX_ORDER_USD} — 차단(백스톱)")
        _j({"event": "BUY_HARD_LIMIT", "sym": sym, "usd": usd}); return 1
    tdep = _today_deployed_usd()             # 오늘 투입 총액 하드 상한(SL10, 저널 예산가드와 독립)
    if tdep + usd > HARD_MAX_DAILY_USD:
        print(f"⛔ 오늘 투입 ${tdep:.0f}+${usd:.0f} > 하드 일일상한 ${HARD_MAX_DAILY_USD} — 차단(백스톱)")
        _j({"event": "BUY_HARD_DAILY", "sym": sym, "usd": usd, "today": round(tdep, 2)}); return 1
    cbl, creason = _in_cooldown(sym)         # 손절 후 재진입 쿨다운(리벤지 방지)
    if cbl:
        print(f"⛔ {sym} {creason}")
        _j({"event": "BUY_COOLDOWN", "sym": sym, "reason": creason}); return 1
    # ── 물타기·종목수·예산 가드 (저널 기반) ──
    occ, deployed = _open_symbols_and_deployed()
    if sym in occ:
        print(f"⛔ {sym} 이미 전략 보유중 — 물타기 금지"); return 1
    if len(occ) >= MAX_POSITIONS:
        print(f"⛔ 전략 보유 {len(occ)}종목 — 최대 {MAX_POSITIONS} 초과 진입 금지"); return 1
    if deployed + usd > BUDGET_USD:
        print(f"⛔ 배치 ${deployed:.0f}+${usd:.0f} > 상한 ${BUDGET_USD:.0f} — 진입 금지"); return 1
    if not _concentration_ok(usd):           # 단일종목 과집중 방지(재분석#129)
        cap = MAX_POSITION_WEIGHT * BUDGET_USD
        print(f"⛔ {sym} ${usd:.0f} > 단일종목 상한 ${cap:.0f}(예산 {MAX_POSITION_WEIGHT * 100:.0f}%) — 과집중 방지")
        _j({"event": "BUY_CONCENTRATION", "sym": sym, "usd": usd}); return 1
    olap, dups = _sector_overlap(sym, occ)   # 섹터 중복 가드
    if olap and len(dups) >= MAX_PER_SECTOR:  # #2(개편): 소프트경고 → 하드캡. 섹터당 상한 도달 시 차단(상관 드로다운 방지)
        print(f"  ⛔ {sym}({SECTORS.get(sym)}) 동일섹터 보유 {','.join(dups)} — 섹터당 최대 {MAX_PER_SECTOR}종목 초과, 진입 금지(분산 강제)")
        _j({"event": "BUY_SECTOR_CAP", "sym": sym, "sector": SECTORS.get(sym), "held": dups}); return 1
    elif olap:                                # 상한 미만이면 경고만(캡>1 튜닝 시 유효)
        print(f"  ⚠️ 섹터중복: {sym}({SECTORS.get(sym)})와 보유 {','.join(dups)} 동일섹터 — 분산 약화 인지하고 진행")
        _j({"event": "BUY_SECTOR_OVERLAP", "sym": sym, "sector": SECTORS.get(sym), "held": dups})
    try:                                     # 실제 USD 매수가능 재확인(E18/MS18 backstop)
        bp = float(S.CLIENT.get_buying_power("USD").get("availableAmount") or 0)
        if bp < usd:
            # 매수여력 부족은 '일시적·곧 해소'(입금 FX 환전 지연 등) — rc=2(대기·재시도)로
            # 분류해 개장매수 큐가 영구삭제되지 않게 한다(감사#242 B2: 오늘밤 입금 시나리오 직결).
            print(f"⏸ USD 매수가능 ${bp:.2f} < ${usd} — 자금 미반영, 대기(다음 사이클 재시도)"); return 2
    except Exception:
        pass                                 # 조회 실패 시 서버 게이트에 위임
    cok, creason = _strategy_circuit()       # 일일 서킷(진입수/손실청산수)
    if not cok:
        print(f"⛔ 서킷: {creason}")
        _j({"event": "BUY_CIRCUIT", "sym": sym, "reason": creason}); return 1
    if _regime_risk_off():                   # 시장 하락장(QQQ 200일선 아래) → 신규 억제(S7)
        print("⛔ 레짐 risk_off(QQQ 200일선 아래) — 신규 진입 억제")
        _j({"event": "BUY_REGIME", "sym": sym}); return 1
    dte = _days_to_earnings(sym)             # 실적 임박/직후 진입 금지(발표 전후 event risk)
    if dte is not None and -EARN_POST_BLOCK_DAYS <= dte <= EARN_BLOCK_DAYS:
        when = f"{dte}일 앞" if dte >= 0 else f"{-dte}일 전 발표(직후 소화기간, C8)"
        print(f"⛔ {sym} 실적 {when} — 발표 전후 진입 금지(갭/IV크러시 위험)")
        _j({"event": "BUY_EARN_BLOCK", "sym": sym, "dte": dte}); return 1
    if dte is None:                          # 실적일 미상 + 큰 갭 = 미상 실적이벤트 의심 → 차단(RB6/C10)
        dc0 = _day_change(sym)[0]
        if _earn_unknown_gap_block(dte, dc0):
            print(f"⛔ {sym} 실적일 미상 + 당일 {dc0:+.1f}% 큰 변동 — 미상 실적 이벤트 의심, 진입 보류")
            _j({"event": "BUY_EARN_UNKNOWN_GAP", "sym": sym, "dayChange": round(dc0, 1)}); return 1
    eok, ereason = _entry_guard(sym)         # 진입 논지 독립 재검증(과열·추세·매수존)
    print(f"  [논지] {ereason}")
    if not eok:
        _j({"event": "BUY_REJECT_THESIS", "sym": sym, "reason": ereason})
        print(f"⛔ {sym} {ereason}"); return 1
    ok, note = intraday_entry_check(sym)     # 프리장/1분봉 진입 타이밍 게이트
    print(f"  [1분봉] {note}")
    if not ok:
        _j({"event": "BUY_WAIT", "sym": sym, "reason": note})
        print(f"⏸ {sym} 진입 대기 — 다음 사이클 재확인"); return 2
    sok, sp = _spread_ok(sym)                 # 스프레드 슬리피지 가드
    if ext:      # 장외 예외진입: 급락 디스로케이션+타이트 스프레드 필수(fail-closed)
        dc_ext, _ = _day_change(sym)
        eok2, ereason2 = _ext_entry_ok(MC.us_session(), sp, dc_ext)
        print(f"  [장외예외] {ereason2}")
        if not eok2:
            _j({"event": "BUY_EXT_REJECT", "sym": sym, "reason": ereason2}); return 1
    if not sok:
        _j({"event": "BUY_WAIT", "sym": sym, "reason": f"스프레드 {sp:.2f}% 넓음"})
        print(f"⏸ {sym} 스프레드 {sp:.2f}% > {MAX_SPREAD_PCT}% — 슬리피지 위험, 보류"); return 2
    if sp is not None:
        print(f"  [스프레드] {sp:.3f}% (양호)")
    if ext:      # 장외는 지정가만: 현재가+0.1% 캡(얇은 호가 시장가 슬리피지 차단)
        cur_ext = float(S.CLIENT.get_price(sym)["lastPrice"])
        lim = round(cur_ext * 1.001, 2)
        qty_ext = round(usd / lim, 6)
        print(f"  [지정가] {qty_ext}주 @ ${lim} (현재가+0.1% 캡)")
        plan = S.t_plan_order(sym, "BUY", "LIMIT", quantity=qty_ext, price=lim)
    else:
        plan = S.t_plan_order(sym, "BUY", "MARKET", order_amount=usd)
    if plan.get("error") or not plan.get("preview_token"):
        print("⛔ plan 거부:", json.dumps(plan.get("error"), ensure_ascii=False)[:150])
        _j({"event": "BUY_REJECT", "sym": sym, "usd": usd, "err": plan.get("error")})
        return 1
    if (Path.home() / ".toss-mcp" / "HALT").exists():   # TOCTOU: place 직전 재확인
        print("🛑 HALT — place 직전 차단"); return 1
    snap = plan.get("snapshot", {}) or {}
    print(f"▶ 매수 {sym} ${usd} — confirm: {plan['confirm_phrase']!r}")
    try:      # ext일 때만 서버 불변식11(개장)에 확장세션 허용 전달 — 반드시 되돌린다(감사#223)
        if ext:
            S.ALLOW_EXTENDED_ORDER = True
        r = S.t_place_order_confirmed(plan["preview_token"], plan["confirm_phrase"])
    finally:
        S.ALLOW_EXTENDED_ORDER = False
    if r.get("error"):
        err = r.get("error") or {}; code = err.get("code")
        if code == "reconcile-required":     # POST 불확실 → 주문이 살아있을 수 있음
            _j({"event": "BUY_UNCERTAIN", "sym": sym, "usd": usd, "err": err})
            _set_halt(f"BUY {sym} POST 결과 불확실(reconcile-required) — 수동 확인")
            print(f"⚠️ {sym} 주문 결과 불확실(네트워크) — HALT 설정, 수동 확인 필요"); return 3
        if code in ("DUPLICATE", "ERR_DUPLICATE"):   # 이미 접수된 주문 존재(rank10)
            exid = (r.get("existing_order_id") or err.get("existing_order_id")
                    or err.get("existingOrderId"))    # 봉투 최상위에 위치(감사 CX#1/MS#7)
            if exid:
                st, fq, fp = _reconcile_fill(exid, tries=2, wait=1.0)
                if fq and fq > 0:      # 실제 체결분만, 멱등 증분 채택(T5/T8: stale-coid 이중롱 차단)
                    # 비종결(working) 상태면 PARTIAL로 기록해 잔량 추적 유지(감사#223)
                    st_rec = st if st in ("FILLED", "PARTIAL_FILLED") else "PARTIAL_FILLED"
                    _record_fill("BUY", sym, exid, st_rec, fq, fp,
                                 extra={"usd": usd, "dup": True})
                    print(f"↔ {sym} 이미 접수된 주문 채택(orderId {exid}, [{st}])"); return 0
                if st in ("CANCELED", "REJECTED", "REPLACED"):   # 죽은 주문 중복 → 유령슬롯 방지(T6/MH6)
                    _j({"event": "BUY_REJECTED", "sym": sym, "usd": usd, "orderId": exid,
                        "status": st, "dup": True})
                    print(f"⛔ {sym} 중복이나 기존 주문 이미 {st}(체결 0) — 종결(슬롯 해제)"); return 1
                _j({"event": "BUY_WORKING", "sym": sym, "usd": usd, "orderId": exid,
                    "status": st, "dup": True})
                print(f"↔ {sym} 기존 주문 진행중(orderId {exid}, [{st}]) — 재확인 대기"); return 0
            _j({"event": "BUY_UNCERTAIN", "sym": sym, "usd": usd, "err": err})
            _set_halt(f"BUY {sym} DUPLICATE인데 기존 주문ID 없음 — 수동 확인")
            print(f"⚠️ {sym} 중복이나 기존 주문ID 없음 — HALT, 수동 확인"); return 3
        print("⛔ 주문 거부:", json.dumps(err, ensure_ascii=False)[:150])
        _j({"event": "BUY_FAIL", "sym": sym, "usd": usd, "err": err})
        return 1
    oid = r.get("orderId")
    if not oid:      # 정상응답인데 orderId 없음(409 idempotent/빈 응답) → phantom 대신 동결(E15/MS15)
        _j({"event": "BUY_UNCERTAIN", "sym": sym, "usd": usd, "note": "no-orderId"})
        _set_halt(f"BUY {sym} 정상응답인데 orderId 없음 — 수동 확인")
        print(f"⚠️ {sym} 응답에 orderId 없음 — HALT, 수동 확인"); return 3
    try:      # 체결확인 자체가 터져도 '주문 나감'은 반드시 기록(감사#227 유령 방지)
        status, fq, fp = _reconcile_fill(oid)
    except Exception as _re:
        _j({"event": "BUY_WORKING", "sym": sym, "orderId": oid,
            "status": None, "note": f"reconcile-exception: {type(_re).__name__}"})
        _set_halt(f"BUY {sym} 체결확인 예외({type(_re).__name__}) — 수동 확인")
        print(f"⚠️ {sym} 체결확인 중 예외 — 저널 기록 후 HALT"); return 3     # 실제 체결 확인(유령 포지션 방지)
    base = {"usd": usd, "snapPrice": snap.get("snapshotPrice"),
            "coid": snap.get("clientOrderId")}    # 서버 감사로그와 조인용(E17/MS17)
    if status in ("FILLED", "PARTIAL_FILLED") and fq > 0:
        _record_fill("BUY", sym, oid, status, fq, fp, extra=base)   # 멱등·PARTIAL 구분(T3/T5)
        # 목표가 자동 배선(감사#243 [A]): 체결가 기준 k×ATR 목표를 원자적 설정 — 신규진입이
        # tgt=None으로 목표익절 분기가 영영 미발동하던 갭 해소. 라운드넘버 바로 아래로 넛지.
        # 이미 목표가 있으면(수동선설정) 덮지 않는다(부분체결 반복 호출·수동우선 보존).
        try:
            if fp and fp > 0 and status == "FILLED" and sym.upper() not in _load_targets():
                raw = _atr_target(fp, _atr_pct(sym))
                # 정수 매도벽 바로 위(≤$0.05)에 걸리면 벽 아래로 넛지($0.03↓). 가격 양수라 int=floor.
                if raw - int(raw) <= 0.05:
                    raw = int(raw) - 0.03
                set_target(sym, round(raw, 2))
        except Exception as _te:
            _j({"event": "TARGET_SET_FAIL", "sym": sym, "err": type(_te).__name__})
            print(f"    ⚠️ {sym} 목표가 자동설정 실패 — 수동 --set-target 필요")
        tag = "부분체결" if status == "PARTIAL_FILLED" else "체결"
        rem = " — 잔량은 다음 사이클 재확인" if status == "PARTIAL_FILLED" else ""
        print(f"✅ {tag} {sym} {fq}주 @ {fp} [{status}]{rem}")
        return 0
    if status in ("REJECTED", "CANCELED"):
        if fq and fq > 0:      # 취소 전 부분체결분 먼저 기록(감사#224: 직접 경로도 유령주식 방지)
            _record_fill("BUY", sym, oid, "PARTIAL_FILLED", fq, fp, extra=base)
            _j({"event": "BUY_REMAINDER_CANCELED", "sym": sym, "orderId": oid, "status": status})
            print(f"⚠️ {sym} 부분체결 {fq}주 후 잔량 {status} — 체결분 기록·잔량 종결"); return 0
        _j({"event": "BUY_REJECTED", "sym": sym, "orderId": oid, "status": status, **base})
        print(f"⛔ 미체결 종결 {sym} [{status}] — 포지션 없음"); return 1
    if status is None:   # 상태조회 전부 실패 — '미체결' 단정 금지(T7/MH4): 체결됐을 수 있음 → 동결
        _j({"event": "BUY_UNCERTAIN", "sym": sym, "orderId": oid, "status": None, **base})
        _set_halt(f"BUY {sym} 체결확인 전부실패(orderId {oid}) — 수동 확인")
        print(f"⚠️ {sym} 체결확인 불가 — HALT, 수동 확인"); return 3
    # 상태 확인됨(working류) → 포지션 확정 안 함, 슬롯만 점유해 재매수 방지
    _j({"event": "BUY_WORKING", "sym": sym, "orderId": oid, "status": status,
        "fillQty": fq, "fillPrice": fp, **base})
    print(f"⏳ {sym} 접수 [{status}] 체결 미확정 — 다음 사이클 재확인(재매수 차단됨)")
    return 0


def sell(sym, qty, reason_cat=None, skip_market_check=False):
    if not _guard(skip_market=skip_market_check):    # manage 경로는 이미 개장확인(SL8)
        return 1
    sym = sym.upper()
    if sym in LEGACY:
        print(f"⛔ {sym} 은 실보유(레거시) — 전략 매도 금지(실계좌 보호)"); return 1
    qty = round(float(qty), 6)
    owned = _strategy_positions().get(sym, {}).get("qty", 0.0)   # 전략 보유분만 매도(E8/CX18)
    if owned <= 1e-9:
        print(f"⛔ {sym} 전략 보유분 없음 — 매도 금지(레거시/오입력 보호)"); return 1
    if qty > owned:
        print(f"  매도 {qty}→{owned:.6f}(전략보유분) 클램프"); qty = round(owned, 6)
    try:                                        # 매도가능 수량으로 추가 클램프(과매도 방지)
        sqd = S.CLIENT.get_sellable_quantity(sym)
        sq = float(sqd.get("sellableQuantity") or sqd.get("quantity") or 0)
        if sq <= 0:
            print(f"⛔ {sym} 매도가능 수량 0 — 중단"); return 1
        if qty > sq:
            print(f"  매도수량 {qty}→{sq}(가능수량) 클램프"); qty = round(sq, 6)
    except Exception:
        pass
    plan = S.t_plan_order(sym, "SELL", "MARKET", quantity=qty)
    if plan.get("error") or not plan.get("preview_token"):
        print("⛔ plan 거부:", json.dumps(plan.get("error"), ensure_ascii=False)[:150])
        return 1
    if (Path.home() / ".toss-mcp" / "HALT").exists():   # TOCTOU: place 직전 재확인
        print("🛑 HALT — 매도 place 직전 차단"); return 1
    print(f"▶ 매도 {sym} {qty}주 — confirm: {plan['confirm_phrase']!r}")
    r = S.t_place_order_confirmed(plan["preview_token"], plan["confirm_phrase"])
    if r.get("error"):
        err = r.get("error") or {}; code = err.get("code")
        if code == "reconcile-required":
            _j({"event": "SELL_WORKING", "sym": sym, "qty": qty, "err": err})
            _set_halt(f"SELL {sym} POST 결과 불확실(reconcile-required) — 수동 확인")
            print(f"⚠️ {sym} 매도 결과 불확실 — HALT, 수동 확인"); return 3
        if code in ("DUPLICATE", "ERR_DUPLICATE"):
            exid = (r.get("existing_order_id") or err.get("existing_order_id")
                    or err.get("existingOrderId"))    # 봉투 최상위에 위치(감사 CX#1/MS#7)
            if exid:
                st, fq, fp = _reconcile_fill(exid, tries=2, wait=1.0)
                if fq and fq > 0:      # 실제 체결분만 멱등 증분 채택(T5)
                    st_rec = st if st in ("FILLED", "PARTIAL_FILLED") else "PARTIAL_FILLED"
                    _record_fill("SELL", sym, exid, st_rec, fq, fp,
                                 extra={"qty": qty, "exitCat": reason_cat, "dup": True})
                    print(f"↔ {sym} 이미 접수된 매도 채택({exid})"); return 0
                if st in ("CANCELED", "REJECTED", "REPLACED"):   # 죽은 매도 채택 → 손절 삼킴 방지(T6/MH6)
                    _j({"event": "SELL_RETRY", "sym": sym, "qty": qty, "orderId": exid,
                        "status": st, "exitCat": reason_cat, "dup": True})
                    print(f"⚠️ {sym} 중복이나 기존 매도 이미 {st}(체결 0) — 미청산, 다음 사이클 재매도")
                    return 1
                _j({"event": "SELL_WORKING", "sym": sym, "qty": qty, "orderId": exid,
                    "status": st, "exitCat": reason_cat, "dup": True})
                print(f"↔ {sym} 기존 매도 진행중({exid}, [{st}]) — 재확인 대기"); return 0
            _j({"event": "SELL_WORKING", "sym": sym, "qty": qty, "err": err, "dup": True})
            _set_halt(f"SELL {sym} DUPLICATE인데 기존 주문ID 없음 — 수동 확인")
            print(f"⚠️ {sym} 매도 중복이나 기존 주문ID 없음 — HALT, 수동 확인"); return 3
        print("⛔ 주문 거부:", json.dumps(err, ensure_ascii=False)[:150])
        _j({"event": "SELL_FAIL", "sym": sym, "qty": qty, "err": err})
        return 1
    oid = r.get("orderId")
    if not oid:      # 정상응답인데 orderId 없음 → phantom 대신 동결(buy E15와 대칭)
        _j({"event": "SELL_WORKING", "sym": sym, "qty": qty, "note": "no-orderId"})
        _set_halt(f"SELL {sym} 정상응답인데 orderId 없음 — 수동 확인")
        print(f"⚠️ {sym} 매도 응답에 orderId 없음 — HALT, 수동 확인"); return 3
    try:      # 체결확인 자체가 터져도 '주문 나감'은 반드시 기록(감사#227 유령 방지)
        st, fq, fp = _reconcile_fill(oid)
    except Exception as _re:
        _j({"event": "SELL_WORKING", "sym": sym, "orderId": oid,
            "status": None, "note": f"reconcile-exception: {type(_re).__name__}"})
        _set_halt(f"SELL {sym} 체결확인 예외({type(_re).__name__}) — 수동 확인")
        print(f"⚠️ {sym} 체결확인 중 예외 — 저널 기록 후 HALT"); return 3     # 실제 체결 확인(실현손익 기록에 필요)
    base = {"qty": qty, "exitCat": reason_cat,
            "coid": (plan.get("snapshot") or {}).get("clientOrderId")}   # 추적성(E17/MS17)
    if st in ("FILLED", "PARTIAL_FILLED") and fq > 0:
        _record_fill("SELL", sym, oid, st, fq, fp, extra=base)   # 멱등·PARTIAL 구분(T3/T5)
        tag = "부분매도" if st == "PARTIAL_FILLED" else "매도체결"
        rem = " — 잔량은 다음 사이클 재확인" if st == "PARTIAL_FILLED" else ""
        print(f"✅ {tag} {sym} {fq}주 @ {fp} [{st}]{rem}"); return 0
    if st in ("REJECTED", "CANCELED"):
        if fq and fq > 0:      # 취소 전 부분체결분 기록(감사#224) — 미기록 시 유령 롱·실현손익 누락
            _record_fill("SELL", sym, oid, "PARTIAL_FILLED", fq, fp, extra=base)
            _j({"event": "SELL_REMAINDER_CANCELED", "sym": sym, "orderId": oid, "status": st})
            print(f"⚠️ {sym} 부분매도 {fq}주 후 잔량 {st} — 체결분 기록·다음 사이클 잔량 재청산")
            return 1        # 전량 청산 실패 → manage가 보유로 계상하고 재시도
        _j({"event": "SELL_REJECTED", "sym": sym, "orderId": oid, "status": st, **base})
        print(f"⛔ 매도 미체결 종결 {sym} [{st}]"); return 1
    if st is None:   # reconcile 전부 실패(주문상태 못 읽음) → 동결(SL1/exec#10, 매도는 특히 유령방지)
        _j({"event": "SELL_WORKING", "sym": sym, "orderId": oid, "status": None, **base})
        _set_halt(f"SELL {sym} 체결확인 전부실패(orderId {oid}) — 수동 확인")
        print(f"⚠️ {sym} 매도 체결확인 불가 — HALT, 수동 확인"); return 3
    _j({"event": "SELL_WORKING", "sym": sym, "orderId": oid, "status": st,
        "fillQty": fq, "fillPrice": fp, **base})
    print(f"⏳ {sym} 매도 접수 [{st}] 체결 미확정 — 다음 사이클 재확인"); return 0


def _idle_capital_alert(tradable):
    """#6(개편): 0포지션인데 배치가능 USD가 놀고 있으면 '스캔 필요' 경보(당일 1회, ET 거래일 래치).
    2026-08 3주 휴면(0포지션·자본 방치) 재발 방지 — 하락장(risk_off)이면 대기가 정상이라 침묵.
    전부 fail-safe: 어떤 예외/미개장이면 조용히 반환(매매 안 막음, 알림만)."""
    try:
        if not tradable:
            return                             # 휴장/장외엔 스캔 무의미 — 정규장 사이클에서만 나그
        bp = float(S.CLIENT.get_buying_power("USD").get("availableAmount") or 0)
        if bp < IDLE_CAPITAL_MIN_USD:          # 놀고 있는 현금이 미미하면 스킵
            return
        if _regime_risk_off():                 # 하락장이면 신규 억제가 정상 → 경보 안 함
            return
        aday = JOURNAL.parent / "idle_alert_day"
        try:
            last = aday.read_text(encoding="utf-8").strip() if aday.exists() else ""
        except Exception:
            last = ""
        if last != _trading_day():
            print(f"  💤 유휴자본 ${bp:.0f} · 0포지션 — 진입 후보 스캔 필요(슬롯 {MAX_POSITIONS}개 비어있음)")
            _j({"event": "IDLE_CAPITAL", "usd": round(bp, 2), "slots": MAX_POSITIONS})
            _alert("IDLE_CAPITAL", f"유휴자본 ${bp:.0f}·0포지션 {MAX_POSITIONS}슬롯 — 스캔 필요")
            _atomic_write(aday, _trading_day())
    except Exception:
        pass


def _conn_alive():
    """인증/연결 생존 확인(개편 2026-09-14): 값싼 authed 호출로 토큰·IP 상태 점검. (ok, detail).
    유동 IP로 연결이 조용히 죽는 걸(403 access_denied) 크론이 큰 경보로 잡게 한다 — 그간 rc=0로
    삼켜져 며칠 방치되던 운영 실패 교정. 인증 무관 예외(네트워크 블립)는 오탐 방지 위해 '통과'."""
    try:
        bp = S.CLIENT.get_buying_power("USD")
        if isinstance(bp, dict) and (bp.get("error") or {}).get("status") == 403:
            return False, "403 access_denied(IP 허용/토큰)"
        return True, "ok"
    except Exception as e:
        m = str(e)
        if "403" in m or "token-error" in m.lower() or "access_denied" in m.lower():
            return False, f"인증실패({m[:60]})"
        return True, "비인증 예외(통과)"


def manage():
    """보유 전략 포지션 점검 → -8% 손절 / RSI>=65 익절(기대감 강할 때) / 실적 임박 청산.
    크론이 매 사이클 호출(수기 판단을 코드화, rank4·11). 레거시는 애초에 포지션에 없음."""
    sess = MC.us_session(); mo, why = _market_open()
    tradable = (sess == "미국 정규장") and (mo is not False)
    status = "정규장·매매가능" if tradable else (why if mo is False else f"{sess}·매도보류")
    print(f"[세션] {sess} | 브로커 {why} | {'🟢 매매가능' if tradable else '🔴 신규/매도 보류'}")
    try:      # heartbeat(N3): 마지막 실행시각 — 죽은 크론/멈춘 루프 감지용
        _atomic_write(JOURNAL.parent / "last_run",
                      datetime.now(MC.KST).isoformat(timespec="seconds"))
    except Exception:
        pass
    alive, why_conn = _conn_alive()        # 연결/인증 죽음 조기감지(개편 2026-09-14): 유동 IP 403이 조용히 방치되던 운영실패 교정
    if not alive:
        print(f"  🔴 연결/인증 죽음 — {why_conn}. 시세·매매 불가, 이번 사이클 중단(포지션은 그대로).")
        aday = JOURNAL.parent / "conn_dead_day"
        try:
            last = aday.read_text(encoding="utf-8").strip() if aday.exists() else ""
        except Exception:
            last = ""
        if last != _trading_day():         # 당일 1회 경보(스팸 방지)
            _j({"event": "CONN_DEAD", "detail": why_conn})
            _alert("CONN_DEAD", f"토스 인증 실패({why_conn}) — .env IP 허용 확인 필요. 매매·감시 정지")
            _atomic_write(aday, _trading_day())
        return 1                           # rc=1로 종료 → 크론 로그가 'rc=0 정상'으로 위장 안 됨
    _repoll_pending()      # 미확정 주문 증분 승격/종결(T4/MH5) — 포지션 계산 '전에'
    pos = _strategy_positions()
    if not pos:
        print("관리할 전략 포지션 없음(전략 보유 0)")
        _idle_capital_alert(tradable)      # #6(개편): 유휴자본 방치 경보 — 3주 휴면 재발 방지
        return 0
    print(f"포지션 관리 ({len(pos)}종목)")
    tot_cost = tot_val = 0.0                   # 포트폴리오 요약(B7)
    winners = []                               # ② 피니시라인용: 청산사유 없이 보유중인 '이익' 포지션(sym, qty, pl)
    for sym, p in pos.items():
        try:      # 심볼별 예외 격리(I5): 한 종목 오류가 전체 관리를 죽이지 않게
            entry = p.get("entryPx"); qty = round(p["qty"], 6)
            try:
                cur = float(S.CLIENT.get_price(sym)["lastPrice"])
                # 가격 온전성 검사(감사#225 치명): 0/음수·전일대비 ±50% 밖 = 피드 오류로 간주.
                # 검증 없이 믿으면 lastPrice=0 한 틱이 전 종목 시장가 청산을 유발한다.
                if cur <= 0:
                    raise ValueError(f"lastPrice={cur} 비정상(피드오류) — 이번 사이클 스킵")
                if entry and not (0.5 * entry <= cur <= 1.5 * entry):
                    raise ValueError(f"현재가 {cur} vs 진입 {entry} 괴리 과대(피드오류 의심) — 스킵")
            except Exception:
                dte = _days_to_earnings(sym)        # 시세실패해도 실적청산은 가격 불필요(exec#16)
                els = _earn_last_session(sym)       # 주말 넘는 마지막세션도 청산(RB8)
                if dte is not None and (0 <= dte <= EARN_EXIT_DAYS or (els and dte >= 0)) and tradable:
                    print(f"  {sym}: 시세실패지만 실적 {dte}일전 → 발표 전 청산")
                    sell(sym, round(p["qty"], 6), reason_cat="EARN", skip_market_check=True)
                else:
                    print(f"  {sym}: 시세 조회 실패 — 스킵")
                    tot_cost += p["cost_usd"]; tot_val += p["cost_usd"]   # 조회실패=원가(중립)
                continue
            pl = (cur / entry - 1) * 100 if entry else None
            bars = _fetch_daily(sym, 200)            # SL7: 종목당 1회 fetch로 rsi/ma150/atr/mfe 공유(~5콜→1콜)
            rsi = _rsi(sym, bars); dte = _days_to_earnings(sym)
            tgt = _load_targets().get(sym)
            ma150 = _ma150(sym, bars); days_held = _trading_days_held(p.get("entryTs")); atr = _atr_pct(sym, bars)   # C4 거래일
            mfe = _mfe_pct(sym, entry, p.get("entryTs"), bars)
            reason = _exit_decision(pl, rsi, dte, cur, tgt, days_held=days_held,
                                    ma150=ma150, atr_pct=atr, mfe_pct=mfe,
                                    earn_last_session=_earn_last_session(sym))
            pls = f"{pl:+.1f}%" if pl is not None else "?"
            rsis = f"{rsi:.0f}" if rsi is not None else "?"
            dtes = f"{dte}d" if dte is not None else "-"
            bar = ""
            if tgt and entry and tgt > entry and cur is not None:   # 목표 진행률 미니바(J6)
                prog = max(0.0, min(1.0, (cur - entry) / (tgt - entry)))
                bar = "[" + "▓" * int(prog * 5 + 0.5) + "░" * (5 - int(prog * 5 + 0.5)) + "]"
            tgts = f" 목표 {tgt:.2f}(+{(tgt/entry-1)*100:.1f}%){bar}" if (tgt and entry) else ""
            ddv = _days_to_dividend(sym)
            div_note = f" 배당락{ddv}d" if (ddv is not None and 0 <= ddv <= 2) else ""   # C9 정보성
            head = (f"  {sym}: {qty}주 진입 {entry:.2f} 현재 {cur:.2f}{tgts} | "
                    f"P/L {pls} RSI {rsis} 실적 {dtes}{div_note}")
            if _sell_fail_streak(sym) >= 3:      # A9: 매도 연속실패 경보(시도는 계속함)
                print(f"  🚨 {sym} 오늘 매도 실패 {_sell_fail_streak(sym)}회 연속 — 주문경로 점검 필요")
                _alert("SELL_FAIL_STREAK", f"{sym} 연속 매도실패")
            if reason:
                print(f"{head} → 🔴매도({reason})")
                cat = _exit_category(reason)
                # 가격기반 청산은 팔기 직전 호가 교차검증(감사#233) — 유령체결발 가짜 손절 차단.
                # 갭관통 경보보다 먼저 해야 한다: 유령 가격으로 '급락 갭' 경보가 원장에 남으면
                # 실제로 일어나지 않은 사건이 기록되고 사후 분석이 오염된다.
                if tradable and cat in _PRICE_DRIVEN_EXITS:
                    ok_book, why_book, mid = _price_corroborated(
                        sym, cur, side=_PRICE_DRIVEN_EXITS[cat])
                    streak = _uncorroborated_streak(sym) if not ok_book else 0
                    if not ok_book and streak + 1 >= UNCORROBORATED_MAX_STREAK:
                        # 감사#235 critical: 여기서 계속 막으면 청산 경로가 영구 소멸한다
                        # (pl이 스탑 아래인 한 _exit_decision은 항상 STOP을 반환해 TIME 분기에
                        # 영원히 도달하지 못하고, 유니버스 80종목 중 75종목은 실적일도 미상이라
                        # EARN 백스톱조차 없다). 유령체결은 단발이라 3연속 재현되지 않는다 —
                        # 3연속은 '호가 API가 죽었다'는 뜻이고, 그때는 종전 정책(호가 문제는
                        # 매도를 막지 않는다)으로 되돌아가는 게 손실 상한이 있는 쪽이다.
                        print(f"    ⚠️ {sym} 청산 보류 {streak}회 연속 — 호가 불능으로 판단, "
                              f"손절 강행({why_book})")
                        _j({"event": "EXIT_FORCED_STALE_BOOK", "sym": sym, "cat": cat,
                            "cur": cur, "streak": streak, "detail": why_book})
                    elif not ok_book:
                        print(f"    ⛔ {sym} 청산 보류({streak + 1}/{UNCORROBORATED_MAX_STREAK}) "
                              f"— {why_book}. 다음 사이클 재확인")
                        _j({"event": "EXIT_UNCORROBORATED", "sym": sym, "cat": cat,
                            "cur": cur, "streak": streak + 1, "detail": why_book})
                        tot_cost += p["cost_usd"]
                        # 기각한 유령가로 평가하면 가짜 PORTFOLIO_ALERT가 당일 래치를 소모해
                        # 진짜 하락 경보를 삼킨다(감사#235). 호가 mid > 원가중립 순으로 폴백.
                        tot_val += (mid * p["qty"]) if mid else p["cost_usd"]
                        continue
                # 갭관통 경보는 실제 매도 시도 시 1회만(감사#223: 휴장·매도실패 사이클마다 반복 방지)
                if "갭관통" in reason and tradable:
                    _j({"event": "GAP_THROUGH_STOP", "sym": sym,
                        "pl": round(pl, 1) if pl is not None else None})
                    print(f"    🚨 갭관통 경보 — {sym} 급락 갭 청산(리뷰 필요)")
                if tradable:      # manage가 이미 개장확인 → sell 중복체크 생략(SL8)
                    rc_sell = sell(sym, qty, reason_cat=cat, skip_market_check=True)
                    if rc_sell != 0:   # 청산 실패 → 여전히 보유 중이므로 포트폴리오에 계상(감사#223)
                        print(f"    ⚠️ {sym} 청산 실패(rc={rc_sell}) — 보유로 계상, 다음 사이클 재시도")
                        tot_cost += p["cost_usd"]; tot_val += cur * p["qty"]
                else:
                    # 소수점 보유분은 토스 규칙상 정규장에만 매매 가능(감사#237: fractional-
                    # quantity-outside-regular-hours, 조건부 주문도 정수주만). 봇은 장외에 못 팔지만,
                    # 트리거가 '진짜'(호가 교차검증 통과)면 사용자가 앱에서 24시간 수동 매도할 수
                    # 있으므로 알림을 보낸다(감사#238). 종목당 하루 1회. 유령 프린트엔 침묵.
                    side = _PRICE_DRIVEN_EXITS.get(cat, "down")
                    if side == "up":
                        # 목표/RSI 익절: 실매수호가가 목표 이상이어야 '진짜 도달'(유령 프린트 배제)
                        try:
                            bb = float((S.CLIENT.get_orderbook(sym) or {}).get("bestBid") or 0)
                        except Exception:
                            bb = 0.0
                        genuine = bool(tgt and bb >= tgt)
                    else:
                        # 손절/추세: 유령 저가 프린트가 아닌 실제 하락인지 교차검증
                        genuine = _price_corroborated(sym, cur, side="down")[0]
                    if genuine and not _offhours_alerted(sym):
                        _j({"event": "EXIT_SIGNAL_OFFHOURS", "sym": sym, "cat": cat,
                            "cur": round(cur, 2), "reason": reason})
                        _alert("EXIT_SIGNAL_OFFHOURS",
                               f"{sym} {reason} — 장외라 봇 매도불가, 앱에서 수동매도 검토")
                        print(f"    📲 {sym} 장외 청산신호 알림 발송(앱에서 수동매도 가능)")
                    else:
                        print("    (장 미개장/휴장 — 소수점 장외 매매불가, 알림만)")
                    tot_cost += p["cost_usd"]; tot_val += cur * p["qty"]  # 보류=여전히 보유
            elif (pl is not None and pl >= PARTIAL_TP_PCT and not _partial_tp_taken(sym)
                    and qty * PARTIAL_TP_FRACTION > 1e-6
                    and _ptp_armed(entry, tgt)):    # 목표가와 충돌하면 미무장(감사#232)
                # 부분익절(C2): 전량청산 사유는 없지만 +5% 도달 → 절반 익절·잔량 러너(이익 잠금+상방 유지)
                half = round(qty * PARTIAL_TP_FRACTION, 6)
                print(f"{head} → 🟡부분익절(+{pl:.1f}%≥{PARTIAL_TP_PCT:.0f}% — {half}주 절반 익절, 잔량 러너)")
                if tradable:      # PTP도 가격기반 → 동일 교차검증(감사#233)
                    # PTP는 익절이라 못 팔아도 손실이 커지지 않는다 → 강행 없이 보류만.
                    ok_book, why_book, mid = _price_corroborated(sym, cur, side="up")
                    if not ok_book:
                        print(f"    ⛔ {sym} 부분익절 보류 — {why_book}. 다음 사이클 재확인")
                        _j({"event": "EXIT_UNCORROBORATED", "sym": sym, "cat": "PTP",
                            "cur": cur, "detail": why_book})
                        tot_cost += p["cost_usd"]
                        tot_val += (mid * p["qty"]) if mid else p["cost_usd"]
                        continue
                if tradable:
                    rc_ptp = sell(sym, half, reason_cat="PTP", skip_market_check=True)
                    if rc_ptp == 0:      # 체결 가정은 성공시에만(감사#223: 실패 시 전량 보유 유지)
                        tot_cost += p["cost_usd"] * (1 - PARTIAL_TP_FRACTION)
                        tot_val += cur * (qty - half)
                        # #5(개편): 러너 상방 열기 — 절반 익절 후 잔량 목표가를 원래 상승폭×RUNNER_TARGET_MULT로 상향.
                        # 목표를 '높이기만' 하므로 조기매도·손실 유발 불가(하락은 트레일링·브레이크이븐·손절이 방어).
                        try:
                            if tgt and entry and tgt > entry:
                                new_tgt = round(entry * (1 + RUNNER_TARGET_MULT * (tgt / entry - 1)), 2)
                                if new_tgt > tgt:
                                    set_target(sym, new_tgt)
                                    print(f"    🏃 {sym} 러너 목표 ${tgt:.2f}→${new_tgt:.2f}(원상승폭×{RUNNER_TARGET_MULT}) — 상방 여유")
                        except Exception:
                            pass         # 목표 상향 실패해도 러너는 기존 목표/트레일링으로 관리(무해)
                    else:
                        print(f"    ⚠️ {sym} 부분익절 실패(rc={rc_ptp}) — 전량 보유로 계상")
                        tot_cost += p["cost_usd"]; tot_val += cur * p["qty"]
                else:
                    print("    (장 미개장 — 부분익절 보류)")
                    tot_cost += p["cost_usd"]; tot_val += cur * p["qty"]
            else:
                print(f"{head} → 🟢보유")
                tot_cost += p["cost_usd"]; tot_val += cur * p["qty"]
                if tradable and pl is not None and pl > 0:   # ② 피니시라인 후보(이익 보유분만 — 손실분은 손절선에 위임)
                    winners.append((sym, qty, cur))           # cur 보관: 매도 직전 호가 교차검증(H-1)용
        except Exception as _se:
            # 조용한 삼킴 금지(감사#227): 청산 판정이 예외로 사라지면 손절이 무기한 미발동된다.
            # 저널+경보로 남겨 재발/누적을 반드시 사람이 보게 한다.
            print(f"  🚨 {sym}: 관리 중 예외({type(_se).__name__}: {str(_se)[:60]}) — 이번 사이클 스킵")
            try:
                _j({"event": "MANAGE_ERROR", "sym": sym,
                    "err": f"{type(_se).__name__}: {str(_se)[:120]}"})
                _alert("MANAGE_ERROR", f"{sym} 관리 예외 — 청산판정 스킵됨")
            except Exception:
                pass
            tot_cost += p["cost_usd"]; tot_val += p["cost_usd"]
    if tot_cost > 0:                           # 포트폴리오 미실현 손익 + 손실경보(B7/T22)
        ppct = (tot_val / tot_cost - 1) * 100
        pusd = tot_val - tot_cost
        alert = " ⚠️경보(-5%↓)" if ppct <= -5 else ""
        print(f"포트폴리오: 평가 ${tot_val:.2f} / 원가 ${tot_cost:.2f} | 미실현 {ppct:+.1f}%(${pusd:+.2f}){alert}")
        for _sy, _sn in _serenity_held_note(set(pos)):   # E3: 보유종목 최근 48h Serenity 언급(캐시 전용)
            print(f"  ℹ️ Serenity[{_sy}]: {_sn}")
        # ② 목표 피니시라인(개편 2026-08-28): (재시작후 실현 + 현재 미실현) ≥ 예산×PORTFOLIO_TP_PCT → 이익 보유분 일괄 익절.
        # 손실 보유분은 각자 손절선(-6%)에 위임. 당일 1회 래치(중복청산·스팸 방지). 정규장에서만(tradable) 발동.
        try:
            # M-1(안전검수): 재시작후 실현만 합산 — KST 생문자열 비교 대신 ET 거래일 버킷(KST/ET 경계 오버카운트 방지)
            rr = sum(t["plUsd"] for t in _realized_trades()
                     if _trading_day(t.get("sellTs")) >= RESTART_TS)
            goal = PORTFOLIO_TP_PCT / 100.0 * BUDGET_USD
            total_pnl = rr + pusd
            if tradable and winners and total_pnl >= goal:
                tpday = JOURNAL.parent / "portfolio_tp_day"
                try:
                    lastp = tpday.read_text(encoding="utf-8").strip() if tpday.exists() else ""
                except Exception:
                    lastp = ""
                if lastp != _trading_day():
                    print(f"  🎯 피니시라인 도달: 실현 ${rr:+.2f} + 미실현 ${pusd:+.2f} = ${total_pnl:+.2f} "
                          f"≥ 목표 ${goal:.2f}(+{PORTFOLIO_TP_PCT:.0f}%) — 이익 보유 {len(winners)}종목 익절 시도")
                    _j({"event": "PORTFOLIO_TP", "realizedUsd": round(rr, 2), "unrealUsd": round(pusd, 2),
                        "totalUsd": round(total_pnl, 2), "goalUsd": round(goal, 2), "syms": [w[0] for w in winners]})
                    _alert("PORTFOLIO_TP", f"목표 +{PORTFOLIO_TP_PCT:.0f}% 도달(${total_pnl:+.2f}) — 이익분 일괄 익절")
                    # H-1(안전검수 HIGH): 매도 직전 종목별 호가 교차검증(감사#233 패턴) — 유령틱 한 방에
                    # 전 이익포지션이 조기청산되는 것 차단. 보류분이 있으면 래치 미기록→다음 사이클 재시도.
                    sold_any = False; deferred = 0
                    for wsym, wqty, wcur in winners:
                        ok_book, why_book, _mid = _price_corroborated(wsym, wcur, side="up")
                        if not ok_book:
                            print(f"    ⛔ {wsym} 피니시라인 익절 보류 — {why_book}. 다음 사이클 재확인")
                            _j({"event": "EXIT_UNCORROBORATED", "sym": wsym, "cat": "PORTFOLIO_TP",
                                "cur": wcur, "detail": why_book}); deferred += 1; continue
                        if sell(wsym, wqty, reason_cat="PORTFOLIO_TP", skip_market_check=True) == 0:
                            sold_any = True
                    if sold_any and deferred == 0:      # 전량 확정청산됐을 때만 당일 래치(보류분 남으면 재시도 유지)
                        _atomic_write(tpday, _trading_day())
        except Exception as _tpe:
            print(f"  ⚠️ 피니시라인 평가 예외({type(_tpe).__name__}) — 스킵")
        if ppct <= -5:
            aday = JOURNAL.parent / "portfolio_alert_day"     # ET일 1회만 경보(T12: 30분마다 스팸 방지)
            try:
                last = aday.read_text(encoding="utf-8").strip() if aday.exists() else ""
            except Exception:
                last = ""
            if last != _trading_day():
                _j({"event": "PORTFOLIO_ALERT", "pnlPct": round(ppct, 1), "pnlUsd": round(pusd, 2)})
                _atomic_write(aday, _trading_day())
        try:      # 에쿼티 시계열 스냅샷(N6: 추세 모니터링·차트용)
            _rotate(JOURNAL.parent / "metrics.jsonl")
            with (JOURNAL.parent / "metrics.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps({"v": 1, "ts": datetime.now(MC.KST).isoformat(timespec="seconds"),
                                    "deployed": round(tot_cost, 2), "unrealPct": round(ppct, 1),
                                    "unrealUsd": round(pusd, 2), "positions": len(pos)},
                                   ensure_ascii=False) + "\n")
        except Exception:
            pass
    return 0


def _report_stats(trades):
    """실현거래 → 통계 dict(승률·평균·PF·기대값·최대낙폭·Sharpe). 빈 리스트면 None(T18)."""
    if not trades:
        return None
    import statistics
    pls_usd = [t["plUsd"] for t in trades]
    pls_pct = [t["plPct"] for t in trades]
    tot = sum(pls_usd)
    gains = sum(u for u in pls_usd if u > 0)
    losses = -sum(u for u in pls_usd if u < 0)
    eq = peak = mdd = 0.0
    for u in pls_usd:            # 실현 에쿼티 곡선 최대낙폭
        eq += u; peak = max(peak, eq); mdd = min(mdd, eq - peak)
    sd = statistics.pstdev(pls_pct) if len(pls_pct) > 1 else 0.0
    return {"n": len(trades),
            "winRate": sum(1 for u in pls_usd if u > 0) / len(trades) * 100,
            "avgPct": sum(pls_pct) / len(pls_pct),
            "pf": (gains / losses) if losses > 0 else float("inf"),
            "totalUsd": tot, "expUsd": tot / len(trades),
            "maxDD": mdd,
            "sharpe": (sum(pls_pct) / len(pls_pct) / sd) if sd > 0 else 0.0}


def exec_open_buys():
    """개장 즉시 매수 실행(감사#241): 사전 검증·승인된 pending_buys 큐를 개장 순간 1분 안에 발사.
    '느릿느릿해서 다 오르고 산다'는 문제 해소 — 느린 검증은 개장 전에 끝났고, 여기선 buy()의
    라이브 게이트(추격차단·스프레드·1분봉·예산·실적)만 최종 통과시켜 개장가에 즉시 체결한다.
    개장 창(22:30~22:40) 1분 크론이 이걸 반복 호출 — 체결/거부는 큐에서 빼고, 조건대기(rc=2)만 유지.
    매도(manage)와 대칭: 매도는 개장 1분감시로 이미 해결, 이건 매수판."""
    sess = MC.us_session(); mo, _why = _market_open()
    tradable = (sess == "미국 정규장") and (mo is not False)
    q = _load_pending_buys()
    if not q:
        print("대기 매수 없음 — 큐 비어있음"); return 0
    print(f"[개장매수] 세션 {sess} | 대기 {len(q)}건: {[it['sym'] for it in q]}")
    # 감사#242 B1: HALT/실주문OFF면 큐를 '보존'한다. 이 검사가 없으면 buy()가 rc=1을 돌리고
    # 아래 else가 큐를 통째로 삭제한다 — 비상정지(touch HALT)가 큐를 멈추는 게 아니라 지우게 됨.
    if (Path.home() / ".toss-mcp" / "HALT").exists() or not S.CFG.allow_live_orders:
        print("  🛑 HALT/실주문OFF — 큐 보존, 발사 보류"); return 0
    if not tradable:
        print(f"  (미개장/{sess} — 매수 대기, 개장 시 발사)"); return 0
    td = _trading_day()
    for it in list(q):
        sym, usd = it["sym"], it["usd"]
        if it.get("day") and it["day"] != td:      # L1: stale(다른 거래일 무장) 항목 폐기
            _disarm_buy(sym)
            _alert("STALE_ARM", f"{sym} 무장일 {it['day']}≠오늘 — stale 큐 폐기")
            print(f"    ⚠️ {sym} stale(무장일 {it['day']}≠{td}) — 큐 제거"); continue
        if _days_to_earnings(sym) is None:         # B3 발사시점 재확인(파일 변경 대비)
            _disarm_buy(sym); print(f"    ⛔ {sym} 실적 미상 — 큐 제거(안전)"); continue
        try:                                    # ③ 개장 갭다운 방어(개편 2026-08-28): 떨어지는 칼에 발사 안 함(큐 유지→다음 분 재시도)
            dcg = _day_change(sym)[0]
            if dcg is not None and dcg <= GAP_DOWN_BLOCK_PCT:
                print(f"    ⏸ {sym} 당일 {dcg:+.1f}% 갭다운(≤{GAP_DOWN_BLOCK_PCT:.0f}%) — 발사 보류, 큐 유지(다음 분 재시도)")
                _j({"event": "BUY_GAP_DOWN_DEFER", "sym": sym, "dayChange": round(dcg, 1)}); continue
        except Exception:
            pass                                # 조회 실패 시 buy()의 라이브 게이트(추격·스프레드)에 위임
        print(f"  ▶ 개장매수 시도 {sym} ${usd}")
        rc = buy(sym, usd)                      # 모든 진입 게이트 통과(추격·실적·과열·예산…)
        if rc == 0:                             # 체결 → 큐 제거
            _disarm_buy(sym); print(f"    ✅ {sym} 개장 체결 — 큐 제거")
        elif rc == 2:                           # 자금미반영/스프레드/1분봉 대기 → 큐 유지, 다음 분 재시도
            print(f"    ⏸ {sym} 조건 대기 — 큐 유지, 다음 분 재시도")
        elif rc == 3:                           # 불확실/HALT → 중단(큐 보존, 수동 확인)
            print(f"    🛑 {sym} 불확실/HALT — 큐 보존, 중단"); break
        else:                                   # rc==1 하드거부(예산/과집중/과열) → 재시도 무의미, 제거
            # B1 재확인: 틱 중간 HALT가 걸렸다면 삭제 말고 보존하며 중단
            if (Path.home() / ".toss-mcp" / "HALT").exists():
                print(f"    🛑 HALT 감지 — 큐 보존, 중단"); break
            _disarm_buy(sym); print(f"    ⛔ {sym} 거부 — 큐 제거(재시도 무의미)")
    return 0


def report():
    """실현손익·승률·기대값·낙폭·Sharpe 리포트(학습 루프). 저널의 실체결가로 FIFO 매칭."""
    trades = _realized_trades()
    print("=" * 58); print("전략 실현손익 리포트 — index_journal.jsonl"); print("=" * 58)
    st = _report_stats(trades)
    if not st:
        print("아직 실현된 거래 없음.")
    else:
        krw_note = ""
        try:      # 원화 병기(J2) — 조회실패 시 조용히 생략
            rate = float(S.CLIENT.get_exchange_rate("USD", "KRW").get("rate") or 0)
            if rate > 0:
                krw_note = f" (≈₩{st['totalUsd'] * rate:+,.0f} @{rate:,.0f})"
        except Exception:
            pass
        comm_pct = float(os.environ.get("TOSS_COMMISSION_PCT", "0.1"))   # J3: 편도 수수료 % 추정
        fees = sum((t["qty"] * t["buyPx"] + t["qty"] * t["sellPx"]) for t in trades) * comm_pct / 100
        print(f"실현거래 {st['n']} | 승률 {st['winRate']:.0f}% | 평균 {st['avgPct']:+.1f}% | "
              f"PF {st['pf']:.2f} | 실현손익 ${st['totalUsd']:+.2f}{krw_note}")
        print(f"수수료 추정 -${fees:.2f}(편도 {comm_pct}%) → 순손익 ≈ ${st['totalUsd'] - fees:+.2f} "
              f"(TOSS_COMMISSION_PCT로 조정)")
        print(f"기대값 ${st['expUsd']:+.2f}/거래 | 최대낙폭 ${st['maxDD']:+.2f} | Sharpe {st['sharpe']:.2f}")
        from collections import defaultdict
        by_cat = defaultdict(list)
        for t in trades:
            by_cat[t.get("exitCat") or "?"].append(t)
        print("청산사유별(어떤 룰이 돈 버는지 학습):")
        for cat, ts in sorted(by_cat.items()):
            w = sum(1 for t in ts if t["plUsd"] > 0)
            net = sum(t["plUsd"] for t in ts)
            avg = sum(t["plPct"] for t in ts) / len(ts)      # #8(개편): 평균%도 표기 — 엣지 두께 확인
            flag = "  ⚠️손실룰(튜닝검토)" if (net < 0 and len(ts) >= 3) else ""   # #8: 표본≥3서 순손실 룰 자동 플래그
            print(f"  {cat:7s}: {len(ts)}건 승률 {w / len(ts) * 100:.0f}% "
                  f"평균 {avg:+.1f}% 손익 ${net:+.2f}{flag}")
        try:      # 최근 7일 창(F5: 주간 학습 리뷰 — 전체 누적과 별도로 최근 성과 추세)
            from datetime import date, timedelta
            cut = str(date.fromisoformat(_trading_day()) - timedelta(days=7))
            recent = [t for t in trades if str(t.get("sellTs", ""))[:10] >= cut]
            if recent:
                w7 = sum(1 for t in recent if t["plUsd"] > 0)
                print(f"최근 7일: {len(recent)}건 승률 {w7 / len(recent) * 100:.0f}% "
                      f"손익 ${sum(t['plUsd'] for t in recent):+.2f}")
        except Exception:
            pass
        for t in trades:
            print(f"  {t['sym']:6s} {t['qty']:.4f}주 {t['buyPx']:.2f}→{t['sellPx']:.2f} "
                  f"{t['plPct']:+.1f}% (${t['plUsd']:+.2f})")
    pos = _strategy_positions()
    if pos:
        print("\n미실현 보유:")
        for s, p in pos.items():
            try:
                cur = float(S.CLIENT.get_price(s)["lastPrice"]); pl = (cur / p["entryPx"] - 1) * 100
                print(f"  {s:6s} {p['qty']:.4f}주 진입 {p['entryPx']:.2f} 현재 {cur:.2f} ({pl:+.1f}%)")
            except Exception:
                print(f"  {s:6s} {p['qty']:.4f}주 진입 {p['entryPx']:.2f}")
    return 0


def holdings():
    """브로커 보유 전체 — 평단·현재가·손익·레거시 태그(rank17). 매도는 전략분만 가능."""
    h = S.CLIENT.get_holdings().get("holdings", [])
    strat = _strategy_positions()
    print(f"브로커 보유 {len(h)}종목:")
    for r in h:
        sym = r.get("symbol"); nm = r.get("name") or sym
        qty = r.get("quantity")
        ap = r.get("averagePrice") or r.get("averagePurchasePrice")
        cp = r.get("currentPrice") or r.get("lastPrice")
        plr = r.get("profitLossRate")
        line = f"  {nm}({sym}): {qty}주"
        if ap:
            line += f" 평단 {ap}"
        if cp:
            line += f" 현재 {cp}"
        if plr is not None:
            try:
                line += f" P/L {float(plr):+.1f}%"
            except Exception:
                pass
        if sym in LEGACY:
            line += "  [LEGACY — 전략 매매금지]"
        elif sym in strat:
            line += f"  [전략보유 {strat[sym]['qty']:.4f}주]"
        else:
            line += "  [미기록 — 수동/MCP 보유(전략 관리 밖, J5)]"
        print(line)


def _status_exit_code(halt, fb, loss_tripped):
    """--status 종료코드: HALT·미병합fallback·손실서킷 중 하나라도면 1(unhealthy), 아니면 0.
    진입상한 도달은 정상(0). 순수함수로 분리해 종료코드 매트릭스 단위테스트(TA16)."""
    return 1 if (halt or fb or loss_tripped) else 0


def status():
    """한눈 헬스체크: 세션·개장·HALT·실주문·포지션·예산·서킷·저널. HALT/서킷/fallback이면 exit 1."""
    halt = (Path.home() / ".toss-mcp" / "HALT").exists()
    sess = MC.us_session(); mo, why = _market_open()
    occ, dep = _open_symbols_and_deployed()
    cok, creason = _strategy_circuit()
    j = _read_journal()
    fb = (JOURNAL.parent / "index_journal.fallback.jsonl").exists()
    tradable = (sess == "미국 정규장") and (mo is not False)
    print("=" * 52); print("execute 상태 (--status)"); print("=" * 52)
    print(f"세션: {sess} | 개장: {why} | {'🟢 매매가능' if tradable else '🔴 보류'}")
    try:      # kill-switch(env TOSS_KILL) 상태 통합 뷰(B3/K6) — HALT와 별개 채널
        import config as _cfgm
        kill = bool(getattr(_cfgm, "kill_active", lambda: False)())
    except Exception:
        kill = False
    print(f"HALT: {'🛑 있음' if halt else '없음'} | kill-switch(env): {'🛑 활성' if kill else '없음'} | "
          f"실주문: {'ON' if S.CFG.allow_live_orders else 'OFF'}")
    try:      # last_run 노화 감시(F8: 죽은 관리크론 조기경보) — 정규장인데 2h↑ 이면 경고
        lr = (JOURNAL.parent / "last_run")
        if lr.exists():
            age_s = (datetime.now(MC.KST)
                     - datetime.fromisoformat(lr.read_text().strip())).total_seconds()
            stale = tradable and age_s > 7200
            print(f"last_run: {age_s / 60:.0f}분 전{' ⚠️ 관리크론 정지 의심(정규장 2h↑)' if stale else ''}")
    except Exception:
        pass
    print(f"포지션: {len(occ)}/{MAX_POSITIONS}종목 {sorted(occ) or '없음'} | 배치 ${dep}/{BUDGET_USD}")
    loss_tripped = (not cok) and ("손실" in creason)
    print(f"서킷: {'여유' if cok else creason + (' 🛑' if loss_tripped else ' (진입상한=정상)')}")
    try:      # 하드백스톱·집중도 스냅샷(T16)
        tdep = _today_deployed_usd()
        print(f"백스톱: 오늘투입 ${tdep:.2f}/{HARD_MAX_DAILY_USD:.0f} | "
              f"단일주문 ≤${HARD_MAX_ORDER_USD:.0f} | 종목비중 ≤{MAX_POSITION_WEIGHT * 100:.0f}%")
        strat = _strategy_positions()
        if strat:
            wmax_s, wmax_p = max(strat.items(), key=lambda kv: kv[1]["cost_usd"])
            print(f"최대비중: {wmax_s} ${wmax_p['cost_usd']:.2f}"
                  f"({wmax_p['cost_usd'] / BUDGET_USD * 100:.0f}% of 예산)")
    except Exception:
        pass
    try:      # 레짐 상태(C6: risk_off면 신규진입 자동억제 — 보유관리는 계속·손절/목표 정상작동)
        ro = _regime_risk_off()
        print(f"레짐: {'🔴 risk_off(QQQ<200MA — 신규억제)' if ro else '🟢 risk_on(QQQ>200MA)'}")
    except Exception:
        pass
    try:      # 서버 일일 주문예산 잔여(B5: safety daily.json — KST일 {krw,count})
        dj = json.loads((Path(os.path.expanduser("~/.toss-trader")) / "daily.json").read_text(encoding="utf-8"))
        kd = datetime.now(MC.KST).strftime("%Y-%m-%d")
        d0 = dj.get(kd) or {}
        used_k = float(d0.get("krw") or 0); used_c = int(d0.get("count") or 0)
        print(f"주문예산(서버): 오늘 ₩{used_k:,.0f}/{S.CFG.max_daily_krw:,.0f} | {used_c}/{S.CFG.max_daily_count}건")
    except Exception:
        pass
    print(f"알림채널: 데스크톱 {'ON' if os.environ.get('TOSS_DESKTOP_ALERTS') == '1' else 'OFF(env TOSS_DESKTOP_ALERTS=1로 활성)'} | alerts.jsonl 상시 기록")
    print(f"저널: {len(j)}줄 | fallback파일: {'⚠️ 있음(미병합 체결?)' if fb else '없음'}")
    if j:
        last = j[-1]
        print(f"  최근이벤트: {last.get('event')} {last.get('sym', '')} @ {last.get('ts', '')}")
    # 진입상한 도달은 정상. HALT·fallback미병합·손실서킷만 unhealthy(exit 1)
    return _status_exit_code(halt, fb, loss_tripped)


def check_journal():
    """저널 무결성(merged main+fallback): 필수키·종목별 매도누적≤매수누적(oversell). 문제시 exit 1.
    fallback에만 남은 체결도 포함해 검사(exec#19/MS#16)."""
    from collections import defaultdict
    evs = _read_journal()      # main+fallback 병합·중복제거·시간순
    problems = []; bought = defaultdict(float); sold = defaultdict(float)
    for i, ev in enumerate(evs, 1):
        e = ev.get("event"); sym = ev.get("sym")
        if e in ("BUY_FILLED", "BUY_PARTIAL"):
            for k in ("fillQty", "fillPrice"):
                if ev.get(k) is None:
                    problems.append(f"{i}: {e} {k} 누락")
            try:
                bought[sym] += float(ev.get("fillQty") or 0)
            except Exception:
                problems.append(f"{i}: fillQty 숫자아님")
        elif e in ("SELL_FILLED", "SELL_PARTIAL"):
            try:
                sold[sym] += float(ev.get("fillQty") or 0)
                if sold[sym] > bought[sym] + 1e-6:
                    problems.append(f"{i}: {sym} 매도누적 {sold[sym]:.4f} > 매수누적 {bought[sym]:.4f} (oversell)")
            except Exception:
                problems.append(f"{i}: SELL fillQty 숫자아님")
    print(f"저널 검증(merged {len(evs)}건) | 문제 {len(problems)}건")
    for p in problems[:20]:
        print("  ⚠️", p)
    return 0 if not problems else 1


def verify_broker():
    """전략 저널 원장 vs 브로커 실제 보유 대조(N1). 원장>브로커(유령 롱) 드리프트시 exit 1.
    E10(uncertain 주문 조정)과 별개 — JSONL 원장과 계좌 진실의 조용한 괴리를 잡는다."""
    strat = _strategy_positions()
    try:
        holds = {r.get("symbol"): float(r.get("quantity") or 0)
                 for r in S.CLIENT.get_holdings().get("holdings", [])}
    except Exception as e:
        print("보유 조회 실패:", e); return 1
    print("=" * 52); print("원장 vs 브로커 대조 (--verify-broker)"); print("=" * 52)
    drift = []
    for sym, p in strat.items():
        if sym in LEGACY:
            continue
        jq = p["qty"]; bq = holds.get(sym, 0.0)
        mark = ""
        if bq + 1e-4 < jq:          # 브로커 < 원장 = 유령 롱(위험)
            drift.append(f"{sym}: 원장 {jq:.4f} > 브로커 {bq:.4f} (유령 롱 의심)"); mark = " ⚠️"
        elif bq > jq + 1e-4:        # 브로커 > 원장 = 미기록 추가매수(T10: MCP/앱 수동거래 의심)
            mark = " ℹ️미기록분?"    # 경고만(전략은 원장분만 매도하므로 안전) — 드리프트 exit엔 미포함
            print(f"    ℹ️ {sym} 브로커 보유가 원장보다 많음 — 수동/MCP 매수 의심(전략은 원장분만 관리)")
        print(f"  {sym}: 원장 {jq:.6f} / 브로커 {bq:.6f}{mark}")
    if drift:
        print("⚠️ 드리프트 발견:")
        for d in drift:
            print("   ", d)
    else:
        print("✅ 원장과 브로커 일치")
    return 0 if not drift else 1


def _pending_working_orders(evs):
    """저널에서 아직 미해결(후속 터미널 이벤트 없음) 주문 추림(RB15/E10, 순수함수).
    미해결 = working/uncertain + '부분체결(잔량 미확정)'(T4/MH2). 같은 orderId의
    FILLED/REJECTED/잔량종결이 이후 있으면 해결로 간주. orderId 없으면 자동해결 불가."""
    WORKING = {"BUY_WORKING", "BUY_UNCERTAIN", "SELL_WORKING",
               "BUY_PARTIAL", "SELL_PARTIAL"}          # 부분체결도 잔량이 살아있는 미해결(T4)
    TERMINAL = {"BUY_FILLED", "SELL_FILLED", "BUY_REJECTED", "SELL_REJECTED",
                "BUY_REMAINDER_CANCELED", "SELL_REMAINDER_CANCELED"}
    resolved = {ev.get("orderId") for ev in evs
                if ev.get("event") in TERMINAL and ev.get("orderId")}
    pending, seen = [], set()
    for ev in evs:
        if ev.get("event") in WORKING:
            oid = ev.get("orderId")
            if (oid is None or oid not in resolved) and (oid is None or oid not in seen):
                # 원본 컨텍스트 보존(감사#223): exitCat 유실 시 재폴링 승격분이 PTP로
                # 인정되지 않아 러너를 반복 매도, usd 유실 시 예산가드 undercount.
                pending.append({"event": ev.get("event"), "sym": ev.get("sym"), "orderId": oid,
                                "exitCat": ev.get("exitCat"), "usd": ev.get("usd"),
                                "qty": ev.get("qty")})
                if oid is not None:
                    seen.add(oid)
    # 같은 orderId에 이후 *_PARTIAL이 있으면 표시(감사#227): pending은 '최초' 이벤트를 담기 때문에
    # 종결 가드를 event명(_PARTIAL)으로 판정하면 WORKING→PARTIAL 주문이 영원히 좀비로 남는다.
    partial_oids = {str(ev.get("orderId")) for ev in evs
                    if str(ev.get("event", "")).endswith("_PARTIAL") and ev.get("orderId")}
    for p in pending:
        p["anyPartial"] = str(p.get("orderId")) in partial_oids
    return pending


def _repoll_pending(max_orders=6):
    """미확정 주문 재조회 → 체결 '증분' 승격 / 죽은 주문 종결(T4/MH5·E4).
    manage 시작마다 호출: 브로커 읽기 + 저널 기록만(신규 주문 없음). 멱등(_record_fill).
    working이 나중에 체결됐는데 원장이 몰라 재매수·유령이 생기는 갭을 닫는다."""
    try:
        pend = [p for p in _pending_working_orders(_read_journal()) if p.get("orderId")]
    except Exception:
        return
    for o in pend[:max_orders]:
        oid = o["orderId"]; sym = o.get("sym") or "?"
        kind = "BUY" if str(o["event"]).startswith("BUY") else "SELL"
        try:
            st, fq, fp = _reconcile_fill(oid, tries=2, wait=1.0)
        except Exception:
            continue
        # 원본 컨텍스트 승계(감사#223): exitCat 없으면 PTP가 재발동해 러너를 반복 매도한다
        # exitCat만 승계(감사#225): usd(주문 전액)를 부분체결 이벤트에 붙이면 원가가 부풀어
        # 가짜 손절이 난다 — 원가는 실체결가/실체결금액에서만 나와야 한다.
        ctx = {"repoll": True}
        if o.get("exitCat") is not None:
            ctx["exitCat"] = o["exitCat"]
        if st in ("FILLED", "PARTIAL_FILLED") and fq and fq > 0:
            d = _record_fill(kind, sym, oid, st, fq, fp, extra=ctx)
            if d > 0:
                print(f"  ↻ {sym} {kind} 재확인: +{d}주 체결 승격 [{st}]")
            if st == "FILLED" and d <= 0 and (o.get("anyPartial")
                                              or _recorded_fill_qty(oid, kind) > 0):
                # 전량이 이미 *_PARTIAL로 기록된 주문 → 종결 이벤트가 없어 영구 pending(슬롯 잠식)
                _j({"event": f"{kind}_REMAINDER_CANCELED", "sym": sym, "orderId": oid,
                    "status": st, "note": "cum-already-recorded"})
                print(f"  ↻ {sym} {kind} 전량 기록완료 — 주문 종결 처리")
        elif st in ("REJECTED", "CANCELED", "REPLACED"):
            # 취소 전 부분체결분이 있으면 '먼저 기록'해야 유령주식(브로커엔 있고 원장엔 없음)을 막는다
            if fq and fq > 0:
                d = _record_fill(kind, sym, oid, "PARTIAL_FILLED", fq, fp, extra=ctx)
                if d > 0:
                    print(f"  ↻ {sym} {kind} 취소 전 체결분 +{d}주 기록(유령 방지)")
            if st == "REPLACED":
                # 정정 주문은 '새 orderId로 살아있을 수 있다' → 슬롯 해제·종결 금지(감사#224).
                # 체결분만 기록하고 미해결로 남겨 수동 확인(--reconcile) 유도.
                print(f"  ⚠️ {sym} {kind} 주문 REPLACED — 후속 주문 생존 가능, 슬롯 유지(수동확인 권장)")
                _alert("ORDER_REPLACED", f"{sym} {kind} orderId {oid} REPLACED — 후속주문 확인 필요")
            elif (fq and fq > 0) or o.get("anyPartial") or _recorded_fill_qty(oid, kind) > 0:
                _j({"event": f"{kind}_REMAINDER_CANCELED", "sym": sym,
                    "orderId": oid, "status": st})
                print(f"  ↻ {sym} 부분체결 잔량 {st} — 잔량 종결(체결분 유지)")
            else:
                _j({"event": f"{kind}_REJECTED", "sym": sym, "orderId": oid,
                    "status": st, "repoll": True})
                print(f"  ↻ {sym} {kind} 미체결 종결 [{st}] — 슬롯 해제")


def reconcile():
    """미해결 working/uncertain 주문을 브로커에 재조회해 '현재 상태'만 보고(READ-ONLY, RB15/E10).
    ⚠️ 저널에 체결을 쓰지 않는다 — 실제 체결 반영은 수동 확인 후(MH 클러스터, 이중기록 방지). 미해결시 exit 1."""
    pend = _pending_working_orders(_read_journal())
    print("=" * 52); print("주문 리컨사일 (--reconcile · READ-ONLY)"); print("=" * 52)
    if not pend:
        print("미해결 working/uncertain 주문 없음 ✅"); return 0
    unresolved = 0
    for o in pend:
        oid = o["orderId"]; sym = o.get("sym") or "?"; ev = o["event"]
        if not oid:
            print(f"  ⚠️ {sym} {ev} — orderId 없음(자동조회 불가, 수동확인)"); unresolved += 1
            continue
        st, fq, fp = _reconcile_fill(oid, tries=2, wait=1.0)     # 브로커 조회(읽기 전용)
        if st in ("FILLED", "PARTIAL_FILLED") and fq and fq > 0:
            print(f"  ✅ {sym} {ev} [{oid}] → 실제 체결 {fq}@{fp} — 저널 미반영, 수동 기록 필요"); unresolved += 1
        elif st in ("REJECTED", "CANCELED"):
            print(f"  ⛔ {sym} {ev} [{oid}] → {st}(미체결 종결) — 정리 가능")
        elif st is None:
            print(f"  ❓ {sym} {ev} [{oid}] → 상태 조회 실패 — 재시도 필요"); unresolved += 1
        else:
            print(f"  ⏳ {sym} {ev} [{oid}] → [{st}] 아직 진행중"); unresolved += 1
    print(f"\n미해결/확인필요 {unresolved}건 — 체결 반영은 수동(MH 클러스터에서 신중히)")
    return 1 if unresolved else 0


def backtest_strategy():
    """청산룰 백테스트(N2, READ-ONLY): 보유 포지션마다 진입 이후 일봉으로 청산룰을 시뮬레이션,
    '이 룰대로면 언제 어떤 사유로 나갔을지'를 보여줌(룰 튜닝 학습용). 주문·기록 없음."""
    pos = _strategy_positions()
    print("=" * 52); print("청산룰 백테스트 (--backtest-strategy · READ-ONLY)"); print("=" * 52)
    if not pos:
        print("보유 포지션 없음"); return 0
    tgts = _load_targets()
    for sym, p in pos.items():
        entry = p.get("entryPx"); ets = p.get("entryTs")
        if not entry:
            print(f"  {sym}: 진입가 미상 — 스킵"); continue
        bars = _fetch_daily(sym, 200)
        ed = _trading_day(ets) if ets else None
        seg = [b for b in bars[:-1]      # 진행중 부분봉 제외 + 진입일 이후만
               if (ed is None or str(b.get("timestamp"))[:10] >= ed)]
        idx, reason, px, pl = _backtest_exit(seg, entry, ets, tgts.get(sym))
        if reason:
            print(f"  {sym}: {idx + 1}거래일차 {px:.2f}({pl:+.1f}%) → {reason}")
        else:
            print(f"  {sym}: 미청산(최근 {px:.2f}, {pl:+.1f}%) — 룰상 아직 보유")
    print("\n(주문·기록 없음 — 룰 튜닝 참고용. 실적일 청산은 시뮬서 제외)")
    return 0


def equity():
    """에쿼티 곡선(F6, READ-ONLY): metrics.jsonl 스냅샷을 ASCII로 — 배치액·미실현% 추세 한눈에."""
    p = JOURNAL.parent / "metrics.jsonl"
    print("=" * 58); print("에쿼티 추세 (--equity · metrics.jsonl)"); print("=" * 58)
    rows = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        print("metrics.jsonl 없음/읽기 실패 — manage가 쌓는 스냅샷 필요"); return 1
    if not rows:
        print("데이터 없음"); return 1
    rows = rows[-40:]                        # 최근 40 스냅샷
    vals = [float(r.get("unrealPct") or 0) for r in rows]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    for r, v in zip(rows, vals):
        bar = "█" * int((v - lo) / span * 30 + 1)
        print(f"  {str(r.get('ts', ''))[5:16]} {v:+5.1f}% |{bar}")
    rt = _realized_trades()
    print(f"\n스냅샷 {len(rows)}개 | 미실현 최근 {vals[-1]:+.1f}% | 실현누적 ${sum(t['plUsd'] for t in rt):+.2f}({len(rt)}건)")
    return 0


def merge_fallback():
    """fallback 저널 병합(A12): critical 기록실패로 fallback에만 남은 체결을 메인 저널로 흡수.
    _read_journal 키((ts,event,sym,orderId)) 기준 메인에 없는 라인만 append(멱등),
    병합 후 fallback은 .merged-<ts>로 보존(삭제 아님 — 감사 가능). 성공 시 exit 0."""
    fb = JOURNAL.parent / "index_journal.fallback.jsonl"
    print("=" * 58); print("fallback 병합 (--merge-fallback)"); print("=" * 58)
    if not fb.exists():
        print("fallback 파일 없음 — 병합할 것 없음 ✅"); return 0
    main_keys = set()
    try:
        if JOURNAL.exists():
            for line in JOURNAL.read_text(encoding="utf-8").splitlines():
                try:
                    ev = json.loads(line)
                    main_keys.add((str(ev.get("ts")), ev.get("event"), ev.get("sym"),
                                   str(ev.get("orderId"))))
                except Exception:
                    continue
    except Exception as e:
        print("메인 저널 읽기 실패:", e); return 1
    added = skipped = bad = 0
    try:
        with JOURNAL.open("a", encoding="utf-8") as out:
            for line in fb.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    bad += 1; continue
                key = (str(ev.get("ts")), ev.get("event"), ev.get("sym"), str(ev.get("orderId")))
                if key in main_keys:
                    skipped += 1; continue
                out.write(line + "\n"); main_keys.add(key); added += 1
        _chmod600(JOURNAL)
        stamp = datetime.now(MC.KST).strftime("%Y%m%dT%H%M%S")
        fb.rename(fb.with_suffix(f".merged-{stamp}"))
        print(f"병합 {added}건 | 중복스킵 {skipped} | 손상 {bad} → fallback은 .merged-{stamp}로 보존")
        print("✅ --status의 fallback 경고 해제됨")
        return 0
    except Exception as e:
        print("병합 실패:", e); return 1


def scan_journal():
    """저널 손상 스캔(G5, READ-ONLY): 파싱 불가 라인 위치·내용 리포트(원본 무수정).
    손상 발견 시 exit 1 — 수동 복구 판단용(백업 .backups/와 대조)."""
    print("=" * 58); print("저널 손상 스캔 (--scan-journal · READ-ONLY)"); print("=" * 58)
    bad = 0
    for path in (JOURNAL, JOURNAL.parent / "index_journal.fallback.jsonl"):
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
                if not ev.get("event"):
                    print(f"  ⚠️ {path.name}:{i} event 필드 없음: {line[:80]}"); bad += 1
            except Exception:
                print(f"  ❌ {path.name}:{i} JSON 파싱 불가: {line[:80]}"); bad += 1
    print(f"\n{'✅ 손상 없음' if bad == 0 else f'⚠️ 문제 {bad}건 — .backups/ 백업과 대조해 수동 복구'}")
    return 0 if bad == 0 else 1


def suggest_targets():
    """목표가 ATR 재산정 제안(C5, READ-ONLY): _atr_target(진입 × (1+2×ATR%)) 제안치를
    현재 목표와 비교해 출력만 — 자동 변경 없음(목표 수정은 --set-target으로 명시적으로).
    감사#244 통일(🟡): 종전 max(4%,1.5×ATR)를 buy()의 자동배선 _atr_target(2×ATR)와 일치시킴."""
    pos = _strategy_positions(); tg = _load_targets()
    print("=" * 58); print("목표가 제안 (--suggest-targets · READ-ONLY)"); print("=" * 58)
    if not pos:
        print("보유 포지션 없음"); return 0
    for sym, p in pos.items():
        entry = p.get("entryPx")
        if not entry:
            continue
        atr = _atr_pct(sym)
        sug = _atr_target(entry, atr)          # buy() 자동배선과 동일 공식(2×ATR)
        base = (sug / entry - 1) * 100
        curt = tg.get(sym)
        mark = ""
        if curt:
            diff = (sug / curt - 1) * 100
            mark = f" (현재 {curt:.2f}, {diff:+.1f}%)" + (" ≈유지 권장" if abs(diff) < 2 else " → 재검토")
        else:
            mark = " (현재 미설정 → 설정 권장)"
        print(f"  {sym}: 진입 {entry:.2f} ATR {atr:.1f}% → 제안 {sug:.2f}(+{base:.1f}%){mark}"
              if atr else f"  {sym}: 진입 {entry:.2f} ATR? → 제안 {sug:.2f}(+{base:.0f}%){mark}")
    print("\n적용은 --set-target SYM PRICE (자동 변경 없음)")
    return 0


def set_target(sym, price):
    """목표가 검증·원자적 설정(G4: 손편집 JSON 파손 → 익절출구 소실 방지)."""
    sym = sym.upper()
    try:
        price = float(price)
        assert price > 0
    except Exception:
        print(f"⛔ 목표가 숫자 아님: {price}"); return 1
    cur = _load_targets()
    cur[sym] = price
    if _atomic_write(TARGETS_FILE, json.dumps(cur, ensure_ascii=False, indent=2) + "\n"):
        print(f"✅ 목표가 설정 {sym} → ${price:.2f} (원자적 저장, 총 {len(cur)}종목)")
        return 0
    print("⛔ 저장 실패"); return 1


def set_earnings(sym, date_str):
    """실적일 검증·원자적 설정(G4: 실적가드/청산의 근거 데이터 보호)."""
    from datetime import date
    sym = sym.upper()
    try:
        d = date.fromisoformat(str(date_str)[:10])
    except Exception:
        print(f"⛔ 날짜 형식 오류(YYYY-MM-DD): {date_str}"); return 1
    try:
        cur = json.loads(EARNINGS_FILE.read_text(encoding="utf-8")) if EARNINGS_FILE.exists() else {}
    except Exception:
        print("⚠️ 기존 earnings 파일 파손 — 새로 시작(백업 권장)")
        cur = {}
    cur[sym] = str(d)
    if _atomic_write(EARNINGS_FILE, json.dumps(cur, ensure_ascii=False, indent=2) + "\n"):
        print(f"✅ 실적일 설정 {sym} → {d} (원자적 저장, 총 {len(cur)}종목)")
        return 0
    print("⛔ 저장 실패"); return 1


def audit_join():
    """서버 감사로그(audit.jsonl) ↔ 전략저널 조인(B2, READ-ONLY): 주문 경로 완전 대사.
    감사로그의 place 주문 중 저널이 모르는 orderId → 'MCP/수동 주문'(역드리프트 원인 후보).
    저널 체결 중 감사로그에 없는 것 → 경로 우회 의심(있으면 안 됨)."""
    ap = Path(os.path.expanduser("~/.toss-trader")) / "audit.jsonl"
    print("=" * 58); print("주문경로 대사 (--audit-join · READ-ONLY)"); print("=" * 58)
    audits = []
    try:
        for line in ap.read_text(encoding="utf-8").splitlines():
            try:
                a = json.loads(line)
            except Exception:
                continue
            if "place" in str(a.get("tool") or "") and a.get("order_id"):
                audits.append(a)
    except Exception as e:
        print("감사로그 읽기 실패:", str(e)[:80]); return 1
    jevs = _read_journal()
    j_oids = {str(e.get("orderId")) for e in jevs if e.get("orderId")}
    j_fills = [e for e in jevs if e.get("event", "").endswith(("_FILLED", "_PARTIAL"))
               and e.get("orderId")]
    a_oids = {str(a.get("order_id")) for a in audits}
    outside = [a for a in audits if str(a.get("order_id")) not in j_oids]
    unaudited = [e for e in j_fills if str(e.get("orderId")) not in a_oids]
    print(f"감사로그 place 주문 {len(audits)}건 | 저널 orderId {len(j_oids)}종")
    if outside:
        print(f"\n⚠️ 저널 밖 주문 {len(outside)}건(MCP/수동 — 전략 원장 무관, 역드리프트 원인 후보):")
        for a in outside[-5:]:
            print(f"  [{str(a.get('timestamp'))[:16]}] {a.get('side')} {a.get('symbol')} "
                  f"amt={a.get('est_amount')} oid={str(a.get('order_id'))[:14]}…")
    else:
        print("✅ 감사로그의 모든 주문이 전략 저널과 일치")
    if unaudited:
        print(f"\n❓ 감사로그에 없는 저널 체결 {len(unaudited)}건(경로 우회 의심 — 점검 필요):")
        for e in unaudited[-5:]:
            print(f"  [{str(e.get('ts'))[:16]}] {e.get('event')} {e.get('sym')} oid={str(e.get('orderId'))[:14]}…")
    else:
        print("✅ 저널의 모든 체결이 감사로그에 존재")
    return 0 if not unaudited else 1


def _selftest_gate(results):
    """자가진단 결과 [(name, ok, critical, detail)] → 종료코드(치명 실패 하나라도면 1). N5 순수판정."""
    return 1 if any((not ok and critical) for _, ok, critical, _ in results) else 0


def selftest():
    """프리플라이트 자가진단(N5): 매매 전 환경·저널·상태 점검. 치명 실패면 exit 1(크론 게이트).
    주문/뮤테이션 없음 — 읽기·로컬 점검만. 크론이 매매 사이클 전에 호출해 나쁜 상태서 진입 차단."""
    print("=" * 52); print("프리플라이트 자가진단 (--selftest)"); print("=" * 52)
    results = []
    def chk(name, ok, critical, detail=""):
        results.append((name, ok, critical, detail))
    try:
        chk("설정 로드", True, True, f"live={bool(S.CFG.allow_live_orders)}")
    except Exception as e:
        chk("설정 로드", False, True, str(e)[:60])
    chk("클라이언트 초기화", S.CLIENT is not None, True, "OK" if S.CLIENT is not None else "None")
    try:
        j = _read_journal(); _strategy_positions()
        chk("저널 파싱/포지션", True, True, f"{len(j)}줄")
    except Exception as e:
        chk("저널 파싱/포지션", False, True, str(e)[:60])
    try:
        chk("실적 파일", True, False, f"{len(_load_earnings())}종목")
    except Exception as e:
        chk("실적 파일", False, False, str(e)[:60])
    try:
        chk("목표가 파일", True, False, f"{len(_load_targets())}종목")
    except Exception as e:
        chk("목표가 파일", False, False, str(e)[:60])
    try:      # 설정파일 파싱 자체 검증(T17: silent-{} 조기 발견)
        raw_t = json.loads(TARGETS_FILE.read_text(encoding="utf-8")) if TARGETS_FILE.exists() else {}
        chk("targets 파싱", True, False, f"{len(raw_t)}종목")
    except Exception as e:
        chk("targets 파싱", False, False, str(e)[:50])
    try:
        raw_e = json.loads(EARNINGS_FILE.read_text(encoding="utf-8")) if EARNINGS_FILE.exists() else {}
        chk("earnings 파싱", True, False, f"{len(raw_e)}종목")
    except Exception as e:
        chk("earnings 파싱", False, False, str(e)[:50])
    try:      # 보유종목 목표가 커버리지(T17: TARGET룰이 돈줄 — 누락 조기경보)
        posn = _strategy_positions(); tgn = _load_targets()
        missing = [s for s in posn if s not in tgn]
        chk("목표가 커버리지", not missing, False,
            ("누락: " + ",".join(missing)) if missing else f"{len(posn)}종목 전부 설정")
    except Exception as e:
        chk("목표가 커버리지", False, False, str(e)[:50])
    halt = (Path.home() / ".toss-mcp" / "HALT").exists()
    chk("HALT 부재", not halt, True, "🛑 HALT 존재!" if halt else "없음")
    fb = (JOURNAL.parent / "index_journal.fallback.jsonl").exists()
    chk("fallback 병합", not fb, True, "미병합 체결 의심!" if fb else "없음")
    bok, bdetail = _verify_backup_checksum()
    chk("백업 무결성", bok, False, bdetail)
    try:      # config sanity(K3): 한도류가 0/음수면 가드가 무의미해짐 — 조기 발견
        ok3 = (float(S.CFG.max_order_krw) > 0 and float(S.CFG.max_daily_krw) > 0
               and int(S.CFG.max_daily_count) > 0 and BUDGET_USD > 0 and MAX_POSITIONS > 0)
        chk("config 한도 sanity", ok3, True,
            f"orderKRW {S.CFG.max_order_krw:,.0f}/dailyKRW {S.CFG.max_daily_krw:,.0f}/cnt {S.CFG.max_daily_count}")
    except Exception as e:
        chk("config 한도 sanity", False, True, str(e)[:50])
    try:      # env 오버라이드 감사(K4): 어떤 TOSS_* 환경변수가 기본값을 바꾸는지 가시화(값은 마스킹)
        envs = sorted(k for k in os.environ if k.startswith("TOSS_"))
        shown = ", ".join(k if not any(x in k for x in ("KEY", "SECRET", "TOKEN"))
                          else f"{k}=***" for k in envs)
        chk("env 오버라이드", True, False, shown or "없음(기본값)")
    except Exception:
        pass
    try:      # 토큰 at-rest 보안(K1 퍼미션·K2 로테이션 리마인더)
        tok = Path(getattr(S.CFG, "data_dir", Path.home() / ".toss-mcp")) / "token.json"
        if not tok.exists():
            chk("토큰 캐시", True, False, "미저장(in-memory, 안전)")
        else:
            import stat as _stat
            import time as _time
            mode = tok.stat().st_mode & 0o777
            perm_ok = mode == 0o600
            age_d = (_time.time() - tok.stat().st_mtime) / 86400
            detail = f"perm {oct(mode)}{'✓' if perm_ok else '❌(0600 필요)'} | {age_d:.0f}일 경과"
            if age_d > 90:
                detail += " ⚠️ 키 로테이션 권장(90일↑)"
            chk("토큰 캐시", perm_ok, False, detail)
    except Exception as e:
        chk("토큰 캐시", True, False, f"확인 불가({str(e)[:40]})")
    lk = _acquire_lock()
    chk("락 획득가능", lk is not None, False, "다른 실행중" if lk is None else "OK")
    if lk:
        try:
            lk.close()
        except Exception:
            pass
    for name, ok, critical, detail in results:
        mark = "✅" if ok else ("🛑" if critical else "⚠️")
        print(f"  {mark} {name}: {detail}")
    rc = _selftest_gate(results)
    print(f"\n{'✅ 프리플라이트 통과' if rc == 0 else '🛑 치명 실패 — 매매 금지'}")
    return rc


if __name__ == "__main__":
    a = sys.argv
    cmd = a[1] if len(a) > 1 else ""
    _lock = None
    if cmd in ("--buy", "--buy-ext", "--sell", "--manage", "--exec-open-buys",
               "--arm-buy", "--disarm-buy"):   # 뮤테이팅 → 단일 실행 락(감사#223, #242 L2)
        _lock = _acquire_lock()
        if _lock is None:
            print("⏭ 다른 execute 실행 진행중(락) — 이번 호출 스킵(이중주문 방지)"); sys.exit(0)
        os.environ["TOSS_EXECUTE_LOCK_HELD"] = "1"   # 서버 MCP 락게이트 통과 표식(T9/MH8)
        _backup_journal()                            # 장부 일일 백업(T14) — 뮤테이팅 전에
    _log(f"START {cmd} {' '.join(a[2:4])}".rstrip())     # 실행 이력(RB13/B3)
    _simple = {"--manage": manage, "--report": report, "--status": status,
               "--check-journal": check_journal, "--verify-broker": verify_broker,
               "--reconcile": reconcile,     # --reconcile은 READ-ONLY(RB15)
               "--selftest": selftest,       # --selftest 프리플라이트(N5)
               "--backtest-strategy": backtest_strategy,   # 청산룰 what-if READ-ONLY(N2)
               "--equity": equity,           # 에쿼티 추세 READ-ONLY(F6)
               "--merge-fallback": merge_fallback,         # fallback 병합(A12)
               "--scan-journal": scan_journal,             # 저널 손상스캔 READ-ONLY(G5)
               "--suggest-targets": suggest_targets,       # 목표가 제안 READ-ONLY(C5)
               "--audit-join": audit_join,                 # 주문경로 대사 READ-ONLY(B2)
               "--exec-open-buys": exec_open_buys,         # 개장 즉시 매수(#241, 큐 발사)
               "--list-armed": lambda: (print("대기 매수:", _load_pending_buys()), 0)[1]}
    if len(a) > 3 and cmd == "--buy":
        rc = buy(a[2], a[3])
    elif len(a) > 2 and cmd == "--arm-buy":          # 개장매수 큐 등록(#241). USD 생략 시 자동 리스크사이징(#243)
        q = _arm_buy(a[2], a[3] if len(a) > 3 else None)
        if q is None:                                # 실적미상/레거시 → 등록 거부(감사#242 B3)
            rc = 1
        else:
            armed = next((it for it in q if it["sym"] == a[2].upper()), {})
            print(f"✅ 개장매수 큐 등록 {a[2].upper()} ${armed.get('usd')} → 대기 {len(q)}건"); rc = 0
    elif len(a) > 2 and cmd == "--suggest-entry":    # 리스크사이징·k×ATR목표 제안 READ-ONLY(#243)
        sym = a[2].upper()
        atr = _atr_pct(sym); px = float(S.CLIENT.get_price(sym)["lastPrice"])
        usd = _position_size_usd(atr); tgt = _atr_target(px, atr)
        print(f"[진입제안 {sym}] 현재 ${px:.2f} ATR {atr}% → 사이즈 ${usd:.0f}({usd/px:.3f}주) "
              f"목표 ${tgt:.2f}(+{(tgt/px-1)*100:.1f}%)")
        rc = 0
    elif len(a) > 2 and cmd == "--disarm-buy":       # 개장매수 큐 제거
        q = _disarm_buy(a[2]); print(f"✅ {a[2].upper()} 큐 제거 → 대기 {len(q)}건"); rc = 0
    elif len(a) > 3 and cmd == "--buy-ext":      # 장외 예외진입(급락+지정가 강제)
        rc = buy(a[2], a[3], ext=True)
    elif len(a) > 3 and cmd == "--sell":
        rc = sell(a[2], a[3])
    elif len(a) > 3 and cmd == "--set-target":       # 목표가 원자적 설정(G4)
        rc = set_target(a[2], a[3])
    elif len(a) > 3 and cmd == "--set-earnings":     # 실적일 원자적 설정(G4)
        rc = set_earnings(a[2], a[3])
    elif cmd == "--holdings":
        holdings(); rc = 0
    elif cmd in _simple:
        rc = _simple[cmd]()
    else:
        print("usage: execute.py --buy SYM USD | --sell SYM QTY | --holdings | --manage | "
              "--report | --status | --check-journal | --verify-broker | --reconcile | "
              "--selftest | --backtest-strategy")
        rc = 0
    _log(f"END {cmd} rc={rc}")
    sys.exit(rc)
