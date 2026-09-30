"""serenity_check.py — 매수/매도(재량 결정) 전 Serenity(@aleabitoreddit) 인사이트 체크.

X 직접 읽기는 막혀있어(로그인벽) 공개 GitHub 아카이브(yan-labs/serenity-aleabitoreddit,
매일 자동 갱신)에서 트윗·종목통계를 읽는다. 읽기 전용 — 주문/기록 안 함.

⚠️ Serenity는 고위험 AI/반도체 공급망 '문샷' 스타일(개별종목 100~1000%도, BKKT -80%도).
   → 개별종목 복사 금지. '테마 온도'와 '우리 종목(NVDA/AVGO 등) 관련 코멘트' 레이더로만 사용.
   코드화 자동청산(-8%손절·목표·RSI·실적)은 규율이므로 이걸로 뒤집지 않는다.

Usage:
  python3.12 serenity_check.py TICKER   # 특정 종목 — 진입/청산 재량결정 전 확인
  python3.12 serenity_check.py          # 최근 활동 + 우리 유니버스 교차
"""
import sys
import os
import re
import json
import time
import urllib.request
from pathlib import Path

REPO = "yan-labs/serenity-aleabitoreddit"
RAW = f"https://raw.githubusercontent.com/{REPO}/main/data"
CACHE = Path(os.path.expanduser("~/.toss-trader/serenity"))
# 우리 보유 + 워치리스트(교차용). 보유는 저널서 자동 병합.
WATCH = {"NVDA", "AVGO", "MRK", "NEE", "XOM", "GOOGL", "CL", "PG", "LLY",
         "WFC", "C", "MSFT", "META", "MU", "TSM"}


def _fetch(name, max_age=6 * 3600):
    """RAW 파일 다운로드(캐시). 실패 시 stale 캐시로 폴백, 그것도 없으면 None."""
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / name
    if f.exists() and (time.time() - f.stat().st_mtime) < max_age:
        return f.read_bytes()
    try:
        req = urllib.request.Request(f"{RAW}/{name}", headers={"User-Agent": "Mozilla/5.0"})
        data = urllib.request.urlopen(req, timeout=60).read()
        f.write_bytes(data)
        if name.endswith("tweets.json"):
            _mirror_snapshot(data)      # E5: 원 저장소 소실 대비 일일 로컬 미러
        return data
    except Exception as e:
        if f.exists():
            print(f"⚠️ 갱신 실패({name}) — 캐시본 사용: {str(e)[:60]}")
            return f.read_bytes()
        print(f"⚠️ 다운로드 실패({name}): {str(e)[:60]}")
        return None


def _tweets():
    raw = _fetch("aleabitoreddit_tweets.json")
    if not raw:
        return []
    try:
        d = json.loads(raw)
        return d if isinstance(d, list) else (d.get("tweets") or [])
    except Exception:
        return []


def _ticker_stats():
    raw = _fetch("ticker_stats.txt")
    stats = {}
    if not raw:
        return stats
    for line in raw.decode("utf-8", "ignore").splitlines():
        p = line.split()
        if len(p) >= 4 and p[1].isdigit():
            stats[p[0].upper()] = {"mentions": int(p[1]), "first": p[2], "last": p[3]}
    return stats


def _mirror_snapshot(data, keep=7):
    """트윗 아카이브 일일 로컬 미러(E5): 원 저장소가 삭제/비공개 전환돼도 마지막 데이터 보존.
    gzip으로 mirror/tweets-YYYYMMDD.json.gz 1일 1회, keep일 보관. 실패 조용히."""
    try:
        import gzip
        from datetime import date
        mdir = CACHE / "mirror"
        mdir.mkdir(parents=True, exist_ok=True)
        dst = mdir / f"tweets-{date.today().strftime('%Y%m%d')}.json.gz"
        if not dst.exists():
            dst.write_bytes(gzip.compress(data))
        old = sorted(mdir.glob("tweets-*.json.gz"))
        for f in old[:-keep]:
            f.unlink()
    except Exception:
        pass


_BULL_KW = ("beat", "hike", "sold out", "soldout", "upward", "raise", "record",
            "production", "shortage", "tight", "bullish", "accelerat", "expand")
_BEAR_KW = ("delay", "miss", "cut", "downward", "weak", "bearish", "cancel",
            "oversupply", "slump", "warning", "downgrade")


def _tone(texts):
    """키워드 기반 강세/약세 톤 집계(E2). 조잡한 근사 — 참고용 신호일 뿐 판단 근거 아님."""
    bull = sum(1 for t in texts for k in _BULL_KW if k in t.lower())
    bear = sum(1 for t in texts for k in _BEAR_KW if k in t.lower())
    return bull, bear


def _archive_staleness_warn():
    """아카이브 신선도 경고(T18): 원 저장소의 heartbeat 동기화가 멈추면 '어제의 인텔'을
    최신인 줄 알고 매매 판단에 쓰게 됨 → 4일 이상 미갱신이면 경고 출력."""
    raw = _fetch("sync_state.json", max_age=3600)
    if not raw:
        print("⚠️ 아카이브 동기화 상태 확인 불가 — 데이터 신선도 미보장")
        return
    try:
        from datetime import datetime, timezone
        st = json.loads(raw)
        lu = str(st.get("last_update_time") or "")
        dt = datetime.fromisoformat(lu.replace("Z", "+00:00"))
        age_d = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
        if age_d > 4:
            print(f"⚠️ 아카이브 {age_d:.0f}일째 미갱신(마지막 {lu[:10]}) — 최신 인텔 아님, 참고 강도 낮출 것")
    except Exception:
        pass


def _txt(t):
    return (t.get("text") or t.get("full_text") or t.get("content") or "")


def _dt(t):
    return str(t.get("createdAtISO") or t.get("createdAt") or t.get("id") or "")


def _held():
    """전략 저널서 현재 보유 티커(교차 강조용). 실패해도 빈 set."""
    try:
        jp = Path(os.path.expanduser("~/.toss-trader/index_journal.jsonl"))
        if not jp.exists():
            return set()
        bought, sold = {}, {}
        for line in jp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except Exception:
                continue
            ev, s = e.get("event"), e.get("sym")
            if not s:
                continue
            if ev in ("BUY_FILLED", "BUY_PARTIAL"):
                bought[s] = bought.get(s, 0) + float(e.get("fillQty") or 0)
            elif ev in ("SELL_FILLED", "SELL_PARTIAL"):
                sold[s] = sold.get(s, 0) + float(e.get("fillQty") or 0)
        return {s for s in bought if bought[s] - sold.get(s, 0) > 1e-6}
    except Exception:
        return set()


def check_ticker(ticker):
    ticker = ticker.upper().lstrip("$")
    stats = _ticker_stats()
    tw = _tweets()
    held = _held()
    tag = " [우리 보유중]" if ticker in held else ""
    print("=" * 58)
    print(f"Serenity 체크: ${ticker}{tag}")
    print("=" * 58)
    _archive_staleness_warn()
    s = stats.get(ticker)
    if s:
        print(f"언급 {s['mentions']}회 | 첫 {s['first']} ~ 최근 {s['last']}")
    else:
        print("Serenity 아카이브에 언급 없음 — 그의 레이더 밖(순수 우리 판단으로 진행)")
    pat = re.compile(r"\$" + re.escape(ticker) + r"\b", re.I)
    hits = sorted([t for t in tw if pat.search(_txt(t))], key=_dt, reverse=True)
    orig = [t for t in hits if not t.get("isRetweet")]
    if orig:
        bull, bear = _tone([_txt(t) for t in orig[:20]])   # E2: 최근 20개 톤 집계
        print(f"톤(키워드 근사): 강세 {bull} / 약세 {bear}"
              + (" → 강세 우위" if bull > bear * 1.5 else
                 (" → 약세 우위" if bear > bull * 1.5 else " → 중립")))
        print(f"\n최근 언급 트윗 {min(6, len(orig))}개(리트윗 제외):")
        for t in orig[:6]:
            reply = "↩" if t.get("isReply") else " "
            print(f"  {reply}[{_dt(t)[:16]}] {_txt(t).replace(chr(10), ' ')[:210]}")
    print("\n⚠️ Serenity=고위험 문샷. 개별종목 복사 금지 — 테마·리스크·타이밍 참고만.")
    print("   코드화 청산룰(-8%/목표/RSI/실적)은 이걸로 뒤집지 않는다.")
    return 0


def overview():
    stats = _ticker_stats()
    tw = sorted(_tweets(), key=_dt, reverse=True)
    held = _held()
    print("=" * 58)
    print("Serenity 최근 활동 + 우리 유니버스 교차")
    print("=" * 58)
    _archive_staleness_warn()
    if tw:
        print(f"아카이브 트윗 {len(tw)} | 최신 {_dt(tw[0])[:16]}")
    print("\n[우리 유니버스 × Serenity 언급빈도]")
    uni = sorted(WATCH | held)
    for tk in uni:
        s = stats.get(tk)
        mark = " ●보유" if tk in held else ""
        if s:
            print(f"  ${tk:6}{mark:5} {s['mentions']:4}회 (최근 {s['last']})")
        else:
            print(f"  ${tk:6}{mark:5}   -   (Serenity 미언급)")
    print("\n[최근 원문 트윗 8개(리트윗 제외)]")
    for t in [x for x in tw if not x.get("isRetweet")][:8]:
        reply = "↩" if t.get("isReply") else " "
        print(f"  {reply}[{_dt(t)[:10]}] {_txt(t).replace(chr(10), ' ')[:150]}")
    print("\n⚠️ 테마 레이더로만 — 개별 문샷 복사 금지. 안정 지향 전략 유지.")
    return 0


def digest():
    """일일 다이제스트(E4): 지난 다이제스트 이후 '새 트윗'만 요약 — 신규 언급 티커 집계 +
    우리 보유/워치 종목 언급은 원문 스니펫으로 하이라이트(E3). 상태는 digest_state.json에 저장."""
    state_p = CACHE / "digest_state.json"
    last_id = 0
    try:
        if state_p.exists():
            last_id = int(json.loads(state_p.read_text(encoding="utf-8")).get("last_id") or 0)
    except Exception:
        pass
    tw = _tweets()
    held = _held()
    uni = WATCH | held
    print("=" * 58); print("Serenity 다이제스트 (--digest · 새 트윗만)"); print("=" * 58)
    _archive_staleness_warn()
    def _id(t):
        try:
            return int(t.get("id") or 0)
        except Exception:
            return 0
    new = sorted([t for t in tw if _id(t) > last_id], key=_id)
    if not new:
        print("지난 다이제스트 이후 새 트윗 없음"); return 0
    tickers = {}
    for t in new:
        for m in re.findall(r"\$([A-Z]{1,5})\b", _txt(t)):
            tickers[m] = tickers.get(m, 0) + 1
    print(f"새 트윗 {len(new)}개 | 언급 티커: "
          + (", ".join(f"${k}×{v}" for k, v in sorted(tickers.items(), key=lambda x: -x[1])[:12]) or "없음"))
    ours = [t for t in new
            if any(re.search(r"\$" + re.escape(u) + r"\b", _txt(t), re.I) for u in uni)]
    if ours:
        print(f"\n📌 우리 보유/워치 관련 {len(ours)}건:")
        for t in ours[:8]:
            mark = "●보유" if any(re.search(r"\$" + re.escape(h) + r"\b", _txt(t), re.I)
                                  for h in held) else "워치"
            print(f"  [{mark}][{_dt(t)[:10]}] {_txt(t).replace(chr(10), ' ')[:180]}")
    else:
        print("우리 보유/워치 종목 언급 없음")
    try:
        state_p.write_text(json.dumps({"last_id": max(_id(t) for t in new)}), encoding="utf-8")
    except Exception:
        pass
    print("\n⚠️ 테마 레이더로만 — 개별 문샷 복사 금지.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--digest":
        sys.exit(digest())
    if len(sys.argv) > 1:
        sys.exit(check_ticker(sys.argv[1]))
    sys.exit(overview())
