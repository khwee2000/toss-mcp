"""risk_manager.py — portfolio-level risk guards for REAL-money readiness.

Every function is a pure, read-only check returning (ok: bool, reason: str) or a
list. The strategy consults check_new_entry() before proposing a buy, the loop
consults check_circuit() each cycle, and the order path consults
check_order_data() right before any live POST. Fail-closed: on doubt, block.

Covers upgrades #1-8 (capital protection) and #11-14 (data/order safety).
"""
from __future__ import annotations
from typing import Dict, List, Tuple

RISK = {
    # --- capital protection (#1-8) ---
    "max_position_pct":   25.0,    # #1 한 종목 최대 비중
    "min_cash_pct":       20.0,    # #2 최소 현금 예비
    "daily_loss_halt_pct": -5.0,   # #3 일일 실현손실 -5% → 신규 중단
    "drawdown_halt_pct":  -15.0,   # #4 고점 대비 -15% → 전체 중단
    "max_positions":       4,      # #5 동시 보유 상한
    "per_trade_risk_pct":  2.0,    # #6 트레이드당 리스크
    "max_deploy_pct":     100.0,   # #7 총 익스포저 캡 (regime이 더 낮추면 그게 우선)
    "max_bucket_pct":     50.0,    # #8 한 섹터/버킷 최대
    # --- data / order safety (#11-14) ---
    "max_spread_pct":      3.0,    # #12 스프레드 상한
    "max_price_age_sec":   180,    # #11 시세 신선도(초)
    "min_order_krw":       20000,  # #13 최소 주문금액(수수료 방어)
}

# #8 sector/bucket map (avoid over-concentration in one theme)
BUCKET = {
    "NVDA": "반도체", "AMD": "반도체", "AVGO": "반도체", "005930": "반도체",
    "000660": "반도체", "MSFT": "빅테크", "AAPL": "빅테크", "GOOGL": "빅테크",
    "META": "빅테크", "AMZN": "빅테크", "NFLX": "빅테크", "TSLA": "전기차",
    "LLY": "헬스케어", "MBRX": "바이오", "COST": "소비", "CRM": "소프트웨어",
    "ORCL": "소프트웨어",
}


def _pct(w) -> float:
    return float(w) * 100.0


def check_new_entry(positions: List[Dict], cash_frac: float, symbol: str,
                    add_weight: float, regime_deploy: float = 1.0) -> Tuple[bool, str]:
    """#1,2,5,7,8 — can we add `add_weight` of `symbol`?  weights are 0..1."""
    held = {p["symbol"]: p.get("weight", 0.0) for p in positions}
    # #1 per-position cap
    new_w = held.get(symbol, 0.0) + add_weight
    if _pct(new_w) > RISK["max_position_pct"]:
        return False, f"종목비중 {(_pct(new_w)):.0f}% > 상한 {RISK['max_position_pct']:.0f}%"
    # #5 max positions
    if symbol not in held and len(positions) >= RISK["max_positions"]:
        return False, f"보유종목수 {len(positions)} ≥ 상한 {RISK['max_positions']}"
    # #2 min cash reserve
    if _pct(cash_frac - add_weight) < RISK["min_cash_pct"]:
        return False, f"현금 {(_pct(cash_frac-add_weight)):.0f}% < 최소예비 {RISK['min_cash_pct']:.0f}%"
    # #7 total deploy cap (regime may tighten it)
    deployed = sum(held.values()) + add_weight
    cap = min(RISK["max_deploy_pct"], regime_deploy * 100.0)
    if _pct(deployed) > cap:
        return False, f"총배치 {(_pct(deployed)):.0f}% > 캡 {cap:.0f}% (regime)"
    # #8 bucket concentration. Unknown symbols are their OWN bucket (falling
    # back to a shared "기타" would falsely lump unrelated names together).
    bkt = BUCKET.get(symbol, symbol)
    bkt_w = add_weight + sum(w for s, w in held.items() if BUCKET.get(s, s) == bkt)
    if _pct(bkt_w) > RISK["max_bucket_pct"]:
        return False, f"'{bkt}' 집중 {(_pct(bkt_w)):.0f}% > 상한 {RISK['max_bucket_pct']:.0f}%"
    return True, "OK"


def check_circuit(daily_pl_pct: float, drawdown_pct: float) -> Tuple[bool, str]:
    """#3,4 — halt trading on daily loss or account drawdown."""
    if daily_pl_pct is not None and daily_pl_pct <= RISK["daily_loss_halt_pct"]:
        return False, (f"🛑 일일손실 {daily_pl_pct:+.1f}% ≤ {RISK['daily_loss_halt_pct']:.0f}% "
                       "→ 오늘 신규매매 중단")
    if drawdown_pct is not None and drawdown_pct <= RISK["drawdown_halt_pct"]:
        return False, (f"🛑 낙폭 {drawdown_pct:+.1f}% ≤ {RISK['drawdown_halt_pct']:.0f}% "
                       "→ 서킷브레이커: 전체 중단")
    return True, "OK"


def check_order_data(price_age_sec, spread_pct, order_krw) -> Tuple[bool, str]:
    """#11,12,13 — refuse to trade on stale/illiquid/too-small conditions."""
    if price_age_sec is not None and price_age_sec > RISK["max_price_age_sec"]:
        return False, f"시세 {int(price_age_sec)}s 경과 > {RISK['max_price_age_sec']}s (오래됨)"
    if spread_pct is not None and spread_pct > RISK["max_spread_pct"]:
        return False, f"스프레드 {spread_pct:.1f}% > {RISK['max_spread_pct']:.0f}% (저유동성)"
    if order_krw is not None and order_krw < RISK["min_order_krw"]:
        return False, f"주문금액 {int(order_krw):,}원 < 최소 {RISK['min_order_krw']:,}원 (수수료 비효율)"
    return True, "OK"


def position_size(account_krw: float, entry: float, stop_pct: float = -7.0) -> Dict:
    """#6 — shares so hitting the stop loses only per_trade_risk% of the account."""
    risk_krw = account_krw * RISK["per_trade_risk_pct"] / 100.0
    per_share = entry * abs(stop_pct) / 100.0
    return {"maxShares": int(risk_krw / per_share) if per_share > 0 else 0,
            "riskKRW": int(risk_krw), "perTradeRiskPct": RISK["per_trade_risk_pct"]}


def validate_config(summary: Dict) -> List[str]:
    """#14 — warn on dangerous settings before going live."""
    w = []
    if summary.get("allow_live_orders") and summary.get("max_order_krw", 0) > 1_000_000:
        w.append("⚠️ 실주문 ON인데 건당한도 100만원↑ — 소액검증 단계엔 과함")
    if summary.get("token_persist"):
        w.append("⚠️ 토큰 디스크 저장 ON — 개인PC 아니면 OFF 권장")
    if summary.get("max_daily_count", 0) > 30:
        w.append("⚠️ 일일주문 30건↑ 허용 — 과매매 위험")
    if summary.get("mode") == "live" and summary.get("allow_live_orders"):
        w.append("🔴 LIVE + 실주문 ON — 실제 돈이 나갑니다. 한도·kill 재확인.")
    return w
