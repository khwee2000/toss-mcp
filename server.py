"""server.py — "내 돈을 아는 AI 트레이더" MCP server (FastMCP / stdio).

Entry point. Registers READ + PLAN tools always. WRITE (order-mutating) tools
are only *registered* when ``TOSS_ALLOW_LIVE_ORDERS=1`` (review-mode default is
read-only). Even when registered, an order only reaches the network in LIVE
mode with a valid preview_token + matching confirm_phrase + kill switch off.

Run:
    python3 server.py            # stdio MCP server (mock by default)
    python3 server.py --selfcheck  # offline smoke test, no mcp SDK needed

Requires the official ``mcp`` SDK to serve (see requirements.txt). The
``--selfcheck`` path and all helper modules import with stdlib only, so this
file py_compiles and self-tests even before ``mcp`` is installed.
"""

from __future__ import annotations

import json
import sys
from typing import Optional

import analytics
import safety
from config import load_config
from indicators import compute
from preview_store import PreviewStore
from toss_client import TossApiError, TossClient

# ---------------------------------------------------------------------------
# Process-lifetime singletons (mode resolved exactly once here)
# ---------------------------------------------------------------------------

CFG = load_config()
CLIENT = TossClient(CFG)
PREVIEWS = PreviewStore(CFG.data_dir, persist=CFG.allow_live_orders)


def _et_regular_now() -> bool:
    """지금이 미국 정규장 시간대(ET 평일 09:30~16:00)인지 — 캘린더 백스톱의 적용범위 제한용.
    백스톱은 sessions를 비워 시간검사를 스킵시키므로, 장전/장후에까지 적용되면 정규장 전용
    규칙이 무력화된다(감사#232). 휴장일 판정은 하지 않는다 — 그건 실체결 백스톱의 몫."""
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        now = _dt.now(ZoneInfo("America/New_York"))
    except Exception:
        return False           # tz DB 없으면 fail-closed(백스톱 미적용 = 더 보수적)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def _market_live_by_trades(symbol: str, max_age_s: int = 300) -> bool:
    """최근 실체결(≤max_age_s초)이 있으면 True. 시장개장 가드(불변식11)의 백스톱.

    실측(2026-07-15): 토스 US market-calendar가 조회한 '오늘'을 상시 isHoliday=True로
    반환하는 오작동이 있어, 실제로 장이 열리고(토스 앱에선 주문 체결됨) 실체결이 초단위로
    쏟아지는데도 check_market_open이 MARKET_CLOSED로 사전차단했다. 실체결 최신성은 신뢰 가능한
    '실제 개장' 신호이므로 캘린더 false-closed 교정에 쓴다. 체결이 없으면(진짜 휴장) False→차단 유지."""
    try:
        from datetime import datetime, timezone
        tr = CLIENT.get_trades(symbol, 3)
        rows = tr.get("trades") or tr.get("data") or []
        ts = max((str(r.get("timestamp") or r.get("time") or "") for r in rows),
                 default="")
        if not ts:
            return False
        last = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        if last.tzinfo is None:      # 토스 naive 타임스탬프는 KST(감사#223: UTC 가정 오류)
            from zoneinfo import ZoneInfo
            last = last.replace(tzinfo=ZoneInfo("Asia/Seoul"))
        _age = (datetime.now(timezone.utc) - last).total_seconds()
        return -120 <= _age <= max_age_s      # 피드 시계앞섬 허용(execute _recent_trade와 동일)
    except Exception:  # noqa: BLE001
        return False
# Stateful order-safety guard (invariants 7/8/9/10). One per process; all state
# under CFG.data_dir (0700). Read tools never touch it; only order tools do.
GUARD = safety.SafetyGuard(CFG)


def _err(exc: Exception) -> dict:
    if isinstance(exc, TossApiError):
        d = exc.to_dict()  # to_dict() already folds requestId into error when set
        d["_mode"] = CFG.mode
        return d
    return {"error": {"code": "tool-error", "message": str(exc)}, "_mode": CFG.mode}


# ---------------------------------------------------------------------------
# Common envelope (catalog §"공통 결과/에러 스키마") for the NEW alpha tools.
# Existing 26 tools keep their original raw-envelope return shape untouched
# (back-compat / selfcheck preserved); only the analytics/plan/alias tools
# added below wrap into {ok, data, _mock} / {ok:false, error:{code,message_ko}}.
# ---------------------------------------------------------------------------

_IS_MOCK = not CFG.live

# Official OrderStatus enum (openapi.json v1.1.1) -> 한글 의미. Terminal states
# and pending-transition states are also used by the cancel/modify safety gate.
ORDER_STATUS_KO = {
    "PENDING": "접수대기",
    "PARTIAL_FILLED": "일부체결",
    "PENDING_CANCEL": "취소대기",
    "PENDING_REPLACE": "정정대기",
    "FILLED": "전량체결",
    "CANCELED": "취소완료",
    "REJECTED": "거부",
    "REPLACED": "정정완료",
    "CANCEL_REJECTED": "취소거부",
    "REPLACE_REJECTED": "정정거부",
}
# Statuses on which a cancel/modify is pointless or unsafe (invariant 6).
_ORDER_TERMINAL = {"FILLED", "CANCELED", "REJECTED", "REPLACED"}
_ORDER_IN_TRANSITION = {"PENDING_CANCEL", "PENDING_REPLACE"}


def _ok(data: dict) -> dict:
    """Wrap a successful payload into the standard envelope."""
    return {"ok": True, "data": data, "_mock": _IS_MOCK}


def _fail(code: str, message_ko: str, **extra) -> dict:
    """Build the standard failure envelope (code + Korean message)."""
    out = {"ok": False, "error": {"code": code, "message_ko": message_ko}}
    if extra:
        out.update(extra)
    return out


def _wrap(result: dict) -> dict:
    """Normalize an analytics-layer dict into the common envelope.

    analytics.* return either a plain data dict or ``{"error":{"code",
    "message_ko"}}``; map both onto {ok,...}. If a result already carries the
    {ok:...} envelope (e.g. a safety check) it is passed through unchanged.
    """
    if isinstance(result, dict) and "ok" in result:
        return result
    if isinstance(result, dict) and isinstance(result.get("error"), dict):
        e = result["error"]
        return _fail(e.get("code", "INVALID_PARAM"),
                     e.get("message_ko") or e.get("message", "오류"),
                     **{k: v for k, v in result.items() if k != "error"})
    return _ok(result)


def _err_envelope(exc: Exception) -> dict:
    """Convert an exception to the common failure envelope (for new tools)."""
    if isinstance(exc, TossApiError):
        out = _fail(exc.code, exc.message or exc.code, _mode=CFG.mode,
                    data=exc.data, status=exc.status)
        if exc.request_id:  # surface Toss requestId for support correlation
            out["error"]["requestId"] = exc.request_id
        return out
    return _fail("INVALID_PARAM", str(exc), _mode=CFG.mode)


def _clamp_ttl(default: float = 120.0) -> float:
    """preview TTL = min(120s, fx validUntil - now) when FX is relevant."""
    return default  # FX clamp applied per-tool where a rate is fetched.


# ===========================================================================
# Tool implementations (pure functions; registered onto FastMCP below)
# ===========================================================================

# ----- READ ---------------------------------------------------------------

def t_get_account_info() -> dict:
    """[READ] 본인 계좌 목록(accountNo/accountSeq/accountType, 정상상태만)."""
    try:
        data = CLIENT.get_accounts()
        for i, a in enumerate(data.get("accounts", [])):
            a["index"] = i  # surface index mapping (not the seq) for selection
        return data
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_resolve_symbol(query: str) -> dict:
    """[READ/로컬] 자연어 종목명 → 토스 심볼/시장/통화. 모호하면 후보만 반환."""
    cands = CLIENT.resolve_symbol(query)
    return {"query": query, "candidates": cands, "ambiguous": len(cands) > 1,
            "_mode": CFG.mode}


def t_get_quote(symbol: str, interval: str = "1d") -> dict:
    """[READ] 단일 종목 종합 시세(현재가 + candles 합성으로 변동률/OHLC 보강)."""
    try:
        price = CLIENT.get_price(symbol)
        candles = CLIENT.get_candles(symbol, interval=interval, count=2)
        rows = candles.get("candles", [])
        prev_close = float(rows[-2]["close"]) if len(rows) >= 2 else None
        last = float(price.get("lastPrice"))
        change = (last - prev_close) if prev_close else None
        change_pct = round(change / prev_close * 100, 2) if prev_close else None
        latest = rows[-1] if rows else {}
        return {
            "symbol": symbol, "lastPrice": price.get("lastPrice"),
            "currency": price.get("currency"), "timestamp": price.get("timestamp"),
            "previousClose": prev_close, "change": change, "changePct": change_pct,
            "open": latest.get("open"), "high": latest.get("high"),
            "low": latest.get("low"), "volume": latest.get("volume"),
            "_mode": CFG.mode,
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_get_prices(symbols: str) -> dict:
    """[READ] 현재가 다건(콤마구분, 최대 200). 관심종목 일괄 조회."""
    try:
        syms = [s.strip() for s in symbols.split(",") if s.strip()][:200]
        return CLIENT.get_prices(syms)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_get_orderbook(symbol: str) -> dict:
    """[READ] 단일 종목 10단계 호가."""
    try:
        return CLIENT.get_orderbook(symbol)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_get_candles(symbol: str, interval: str = "1d", count: int = 100,
                  before: Optional[str] = None, adjusted: bool = True) -> dict:
    """[READ] 단일 종목 캔들 OHLCV. interval 1m|1d, count 1~200."""
    try:
        return CLIENT.get_candles(symbol, interval, count, before, adjusted)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_get_trades(symbol: str, count: int = 50) -> dict:
    """[READ] 단일 종목 최근 체결(체결가/체결량/시각)."""
    try:
        return CLIENT.get_trades(symbol, count)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_get_price_limits(symbol: str) -> dict:
    """[READ] 단일 종목 당일 상/하한가."""
    try:
        return CLIENT.get_price_limits(symbol)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_get_stock_warnings(symbol: str) -> dict:
    """[READ] 단일 종목 매수 유의(투자경고/위험/VI 등)."""
    try:
        return CLIENT.get_warnings(symbol)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_stock_info(symbols: str) -> dict:
    """[READ] 종목 마스터(상장상태·ETF여부·시총·레버리지·거래정지/정리매매).

    symbols: 콤마구분 종목코드/티커(최대 50, 예: "005930,NVDA"). 각 종목의
    상장정보(market·securityType·isCommonShare)·상장/상폐일·발행주식수, 그리고
    시가총액(=sharesOutstanding×현재가)·레버리지배수·국내 거래정지/정리매매
    플래그(koreanMarketDetail)를 반환. 주문 직전 거래가능 여부 점검에 유용.
    """
    try:
        syms = [s.strip() for s in symbols.split(",") if s.strip()][:50]
        if not syms:
            return _fail("INVALID_PARAM", "symbols가 비어 있습니다")
        env = CLIENT.get_stocks(syms)
        rows = env.get("stocks", []) or []
        out = []
        for r in rows:
            ccy = (r.get("currency") or "").upper()
            kd = r.get("koreanMarketDetail") or {}
            # 시가총액 = 발행주식수 × 현재가. currentPrice may ride along (mock);
            # otherwise fetch the live price for this symbol.
            shares = None
            try:
                shares = float(r.get("sharesOutstanding"))
            except (TypeError, ValueError):
                shares = None
            px = None
            try:
                px = float(r.get("currentPrice"))
            except (TypeError, ValueError):
                px = None
            if px is None:
                try:
                    px = float(CLIENT.get_price(r.get("symbol")).get("lastPrice"))
                except Exception:  # noqa: BLE001
                    px = None
            market_cap = round(shares * px) if (shares and px) else None
            out.append({
                "symbol": r.get("symbol"),
                "name": r.get("name"),
                "market": r.get("market"),
                "securityType": r.get("securityType"),
                "isETF": (r.get("securityType") == "ETF"
                          or r.get("isCommonShare") is False),
                "isCommonShare": r.get("isCommonShare"),
                "status": r.get("status"),
                "currency": ccy or None,
                "listDate": r.get("listDate"),
                "delistDate": r.get("delistDate"),
                "sharesOutstanding": r.get("sharesOutstanding"),
                "leverageFactor": r.get("leverageFactor"),
                "isLeveraged": r.get("leverageFactor") not in (None, "", "1.0"),
                "currentPrice": px,
                "marketCap": market_cap,
                "liquidationTrading": kd.get("liquidationTrading"),
                "krxTradingSuspended": kd.get("krxTradingSuspended"),
                "nxtTradingSuspended": kd.get("nxtTradingSuspended"),
                "nxtSupported": kd.get("nxtSupported"),
                "tradingSuspended": bool(kd.get("krxTradingSuspended")
                                         or kd.get("nxtTradingSuspended")),
            })
        return _wrap({"stocks": out, "count": len(out)})
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


def t_get_exchange_rate(base_currency: str, quote_currency: str,
                        datetime: Optional[str] = None) -> dict:
    """[READ] KRW↔USD 참고 표시환율(validUntil 노출). 체결가 보장 아님."""
    try:
        return CLIENT.get_exchange_rate(base_currency, quote_currency, datetime)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_get_market_calendar(region: str, date: Optional[str] = None) -> dict:
    """[READ] KR(KRX·NXT)/US 장 운영·휴장·세션(KST)."""
    try:
        return CLIENT.get_market_calendar(region, date)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_get_holdings(symbol: Optional[str] = None, account_index: int = 0) -> dict:
    """[READ] 보유종목 상세(수량/평단/평가/손익/일손익/cost + 합산)."""
    try:
        return CLIENT.get_holdings(symbol)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_get_buying_power(currency: str, account_index: int = 0) -> dict:
    """[READ] 통화별 매수가능 금액(현금 기반)."""
    try:
        return CLIENT.get_buying_power(currency)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_get_sellable_quantity(symbol: str, account_index: int = 0) -> dict:
    """[READ] 특정 종목 매도가능 수량."""
    try:
        return CLIENT.get_sellable_quantity(symbol)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_get_commission_policy(account_index: int = 0) -> dict:
    """[READ] 시장별 매매 수수료(런타임 조회, 하드코딩 금지)."""
    try:
        return CLIENT.get_commissions()
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_get_orders(status: str = "OPEN", symbol: Optional[str] = None,
                 limit: int = 20, account_index: int = 0) -> dict:
    """[READ] 주문 목록(OPEN|CLOSED). execution·페이지네이션 포함."""
    try:
        return CLIENT.list_orders(status, symbol=symbol, limit=limit)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_track_order(order_id: Optional[str] = None,
                  client_order_id: Optional[str] = None,
                  account_index: int = 0) -> dict:
    """[READ] 단일 주문 상태 추적(10 OrderStatus·execution·settlementDate)."""
    if not order_id and not client_order_id:
        return {"error": {"code": "bad-args",
                          "message": "order_id 또는 client_order_id 필요"},
                "_mode": CFG.mode}
    try:
        order = CLIENT.get_order(order_id=order_id, client_order_id=client_order_id)
        # Interpret the 10-value OrderStatus enum into 한글 의미 + lifecycle flags.
        status = (order or {}).get("status")
        if status:
            order["statusKo"] = ORDER_STATUS_KO.get(status, status)
            order["isTerminal"] = status in _ORDER_TERMINAL
            order["isCancelable"] = status not in (
                _ORDER_TERMINAL | _ORDER_IN_TRANSITION)
        return order
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ----- COMPOSITE READ -----------------------------------------------------

def t_portfolio_overview(account_index: int = 0,
                         currency: str = "KRW") -> dict:
    """[합성 READ] '내 계좌 어때?'를 1콜로(accounts+holdings+buying-power+commissions)."""
    try:
        accounts = CLIENT.get_accounts()
        holdings = CLIENT.get_holdings()
        buying = CLIENT.get_buying_power(currency)
        commissions = CLIENT.get_commissions()
        summary = holdings.get("summary", {}) or {}
        # 오늘 손익/등락률 surfaced explicitly from the holdings summary
        # (live: dailyProfitLoss.{amount.krw, rate}; mock: dailyProfitLossKRW).
        today_pnl = {
            "dailyProfitLossKRW": summary.get("dailyProfitLossKRW"),
            "dailyProfitLossRate": summary.get("dailyProfitLossRate"),
        }
        return {
            "accounts": accounts.get("accounts", []),
            "summary": summary,
            "todayPnL": today_pnl,
            "holdings": holdings.get("holdings", []),
            "buyingPower": buying,
            "commissionPolicy": commissions.get("policies", []),
            "_mode": CFG.mode,
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_analyze_symbol(query: str, mode: str = "swing",
                     interval: str = "1d") -> dict:
    """[합성 READ] '엔비디아 분석해줘' — quote+candles+trades+warnings+지표 합성."""
    try:
        cands = CLIENT.resolve_symbol(query)
        if not cands:
            return {"error": {"code": "unresolved",
                              "message": f"'{query}' 종목 해석 실패"},
                    "_mode": CFG.mode}
        if len(cands) > 1:
            return {"ambiguous": True, "candidates": cands,
                    "message": "여러 종목 매칭 — 심볼을 특정해 주세요",
                    "_mode": CFG.mode}
        sym = cands[0]["symbol"]
        # 250 daily bars: enough for ma120 / 52w / 1y / slope(140) / scalpScore.
        candles = CLIENT.get_candles(sym, interval=interval, count=200)
        trades = CLIENT.get_trades(sym, count=50)
        warnings = CLIENT.get_warnings(sym)
        # Upgraded indicators: pass the live current price so changePct/disp/
        # scalpScore reflect the real last price, not just the last close.
        try:
            current = float(CLIENT.get_price(sym).get("lastPrice"))
        except (TypeError, ValueError, KeyError, TossApiError):
            current = None
        ind = compute(candles.get("candles", []), trades.get("trades", []),
                      current_price=current)
        return {
            "resolved": cands[0], "mode": mode, "interval": interval,
            "indicators": ind, "warnings": warnings.get("warnings", []),
            "_mode": CFG.mode,
            "_chain": ("단타 가능성은 scalp_score, 매수 영향은 impact_of_order, "
                       "실주문 직전엔 plan_order로 이어가세요."),
            "_disclaimer": "참고용 지표 — 투자 판단·책임은 계좌주에게 있습니다.",
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ----- ALPHA READ (analytics.py; common {ok,data,_mock} envelope) ----------

def _resolve_one(query: str):
    """Resolve a query to a single symbol or return (None, envelope).

    On ambiguity / no match returns ``(None, failure_envelope)`` so the caller
    can early-return the common envelope.
    """
    cands = CLIENT.resolve_symbol(query)
    if not cands:
        return None, _fail("INVALID_PARAM", f"'{query}' 종목 해석 실패")
    if len(cands) > 1:
        return None, _fail("INVALID_PARAM",
                           "여러 종목 매칭 — 심볼을 특정해 주세요",
                           candidates=cands)
    return cands[0], None


def t_orderbook_pressure(symbol: str, top_n: int = 5) -> dict:
    """[READ] 호가 매수/매도 불균형비·스프레드·상위 N호가 벽·체결강도.

    호가(/orderbook)+체결(/trades)로 단기 수급 압력을 계산. 단타 진입 판단용.
    """
    try:
        sym, fail = _resolve_one(symbol)
        if fail:
            return fail
        s = sym["symbol"]
        ob = CLIENT.get_orderbook(s)
        trades = CLIENT.get_trades(s, count=50)
        return _wrap(analytics.orderbook_pressure(ob, trades, top_n=int(top_n)))
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


def t_intraday_vwap(symbol: str, aggregate_5m: bool = True) -> dict:
    """[READ] 당일 VWAP·vwapGap(%)·당일 레인지 내 위치. '지금 VWAP 위야 아래야?'

    1분봉(/candles interval=1m)을 5분봉으로 집계해 VWAP을 계산. 전일종가/현재가 보강.
    """
    try:
        sym, fail = _resolve_one(symbol)
        if fail:
            return fail
        s = sym["symbol"]
        candles = CLIENT.get_candles(s, interval="1m", count=200)
        price = CLIENT.get_price(s)
        prev_close = None
        try:
            prev_close = float(price.get("previousClose"))
        except (TypeError, ValueError):
            prev_close = None
        current = None
        try:
            current = float(price.get("lastPrice"))
        except (TypeError, ValueError):
            current = None
        return _wrap(analytics.intraday_vwap(
            candles, prev_close=prev_close, current_price=current,
            aggregate_5m=bool(aggregate_5m)))
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


def t_recent_trades_tape(symbol: str, count: int = 50,
                         big_trade_mult: float = 3.0) -> dict:
    """[READ] 최근 체결 테이프: 체결강도·대량체결 표식·틱 흐름. 호가와 짝."""
    try:
        sym, fail = _resolve_one(symbol)
        if fail:
            return fail
        trades = CLIENT.get_trades(sym["symbol"], count=int(count))
        return _wrap(analytics.recent_trades_tape(
            trades, big_trade_mult=float(big_trade_mult)))
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


def t_price_limit_proximity(symbol: str) -> dict:
    """[READ] 상/하한가 근접도(%)·잔여폭. 상따 모니터링(KRX 전용 라벨, US 참고)."""
    try:
        sym, fail = _resolve_one(symbol)
        if fail:
            return fail
        s = sym["symbol"]
        limits = CLIENT.get_price_limits(s)
        price = CLIENT.get_price(s)
        current = None
        try:
            current = float(price.get("lastPrice"))
        except (TypeError, ValueError):
            current = None
        return _wrap(analytics.price_limit_proximity(
            limits, current, symbol=s, currency=sym.get("currency")))
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


def t_scalp_score(symbols: str, interval: str = "1d") -> dict:
    """[READ] 단/다건 단타 적합도 0~100·S~D 등급+사유. '지금 단타 칠 만한 종목?'

    symbols: 콤마구분 종목코드/티커(예: "005930,NVDA"). 각 종목 일봉+현재가 기반.
    """
    try:
        raw = [s.strip() for s in symbols.split(",") if s.strip()][:50]
        if not raw:
            return _fail("INVALID_PARAM", "symbols가 비어 있습니다")
        sym_candles, sym_prices, sym_trades = {}, {}, {}
        for q in raw:
            sym, fail = _resolve_one(q)
            if fail:
                continue  # skip unresolved symbols, keep the rest
            s = sym["symbol"]
            sym_candles[s] = CLIENT.get_candles(s, interval=interval, count=200)
            try:
                sym_prices[s] = float(CLIENT.get_price(s).get("lastPrice"))
            except (TypeError, ValueError):
                pass
            sym_trades[s] = CLIENT.get_trades(s, count=50)
        if not sym_candles:
            return _fail("INVALID_PARAM", "해석 가능한 종목이 없습니다")
        return _wrap(analytics.scalp_score(sym_candles, sym_prices, sym_trades))
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


def t_supply_demand_flow(symbol: str) -> dict:
    """[READ] 외국인/기관/개인 순매수 억원·추세. '외국인이 사고 있어?'

    토스 Open API 미제공 → mock은 합성(_source=mock), live는 stock-dashboard
    (Naver) 보조 소스 필요. live에서 보조 소스 없으면 null(추측 금지).
    """
    try:
        sym, fail = _resolve_one(symbol)
        if fail:
            return fail
        s = sym["symbol"]
        # mock backend synthesizes a clearly-labelled flow; live must source
        # externally (Naver) and never fabricate. Pull a naver-shaped dict from
        # the mock backend when available; otherwise pass None (live -> nulls).
        naver_flow = None
        mock = getattr(CLIENT, "_mock", None)
        if mock is not None and hasattr(mock, "supply_demand_flow"):
            raw = mock.supply_demand_flow(s)
            latest = raw.get("latest") if isinstance(raw, dict) else None
            if latest:
                naver_flow = {
                    "date": latest.get("date"),
                    "foreignVal": latest.get("foreignVal"),
                    "instVal": latest.get("instVal"),
                    "indivVal": latest.get("indivVal"),
                    "foreignHold": raw.get("foreignHoldRate"),
                }
        out = analytics.supply_demand_flow(
            s, naver_flow=naver_flow, mode=CFG.mode)
        # In mock mode tag the synthetic source honestly.
        if _IS_MOCK and naver_flow is not None:
            out["_source"] = "mock"
        return _wrap(out)
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


def t_rank_watchlist(symbols: Optional[str] = None, top_n: int = 5,
                     sort_by: str = "scalpScore", interval: str = "1d") -> dict:
    """[READ] 워치리스트 TOP N 단타 후보 + 한 줄 브리핑. '오늘 뭐 봐야 해?'

    symbols 미지정 시 mock watchlist 상위 일부를 사용. sort_by:
    scalpScore|changePct|volValue|rsi|volRatio.
    """
    try:
        if symbols:
            raw = [s.strip() for s in symbols.split(",") if s.strip()]
        else:
            mock = getattr(CLIENT, "_mock", None)
            if mock is not None and hasattr(mock, "watchlist"):
                raw = [w["symbol"] for w in mock.watchlist(limit=12)]
            else:
                raw = []
        raw = raw[:30]
        if not raw:
            return _fail("INVALID_PARAM",
                         "워치리스트가 비었습니다 — symbols를 지정하세요")
        sym_candles, sym_meta, sym_prices = {}, {}, {}
        for q in raw:
            sym, fail = _resolve_one(q)
            if fail:
                continue
            s = sym["symbol"]
            sym_candles[s] = CLIENT.get_candles(s, interval=interval, count=200)
            sym_meta[s] = {"name": sym.get("name", s),
                           "currency": sym.get("currency")}
            try:
                sym_prices[s] = float(CLIENT.get_price(s).get("lastPrice"))
            except (TypeError, ValueError):
                pass
        if not sym_candles:
            return _fail("INVALID_PARAM", "해석 가능한 종목이 없습니다")
        return _wrap(analytics.rank_watchlist(
            sym_candles, symbol_meta=sym_meta, symbol_prices=sym_prices,
            top_n=int(top_n), sort_by=sort_by))
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


def t_portfolio_risk_xray(account_index: int = 0,
                          max_single_weight: float = 0.30) -> dict:
    """[READ] 집중도(허핀달)·단일종목 최대비중·시장/통화/현금 비중·위험 플래그.

    '내 포트 한쪽 쏠렸어?' 보유(/holdings)+예수금(/buying-power)+환율(/exchange-rate).
    """
    try:
        holdings = CLIENT.get_holdings()
        cash = None
        try:
            cash = float(CLIENT.get_buying_power("KRW").get("availableAmount"))
        except (TypeError, ValueError, TossApiError):
            cash = None
        usdkrw = _usdkrw_rate()
        return _wrap(analytics.portfolio_risk_xray(
            holdings, buying_power_krw=cash, usdkrw=usdkrw,
            max_single_weight=float(max_single_weight)))
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


def t_pnl_attribution(account_index: int = 0, top_n: int = 5) -> dict:
    """[READ] 종목별 손익 기여도 TOP·손익 귀속 요약. 잔고 뷰어를 스토리텔링으로."""
    try:
        holdings = CLIENT.get_holdings()
        return _wrap(analytics.pnl_attribution(
            holdings, usdkrw=_usdkrw_rate(), top_n=int(top_n)))
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


def t_impact_of_order(symbol: str, side: str, quantity: float,
                      price: Optional[float] = None,
                      currency: Optional[str] = None,
                      account_index: int = 0) -> dict:
    """[READ/what-if] 가상 주문이 집중도·현금비중·통화·손익을 어떻게 바꾸는지 계산.

    ⚠️ 실행 없음. 통화/시장 정합(불변식12) 위반은 CURRENCY_MISMATCH 거부.
    price 미지정 시 현재가 사용. 이후 plan_order로 실주문 미리보기를 이어가세요.
    """
    try:
        sym, fail = _resolve_one(symbol)
        if fail:
            return fail
        s = sym["symbol"]
        cur = (currency or sym.get("currency") or CLIENT.currency_for(s)).upper()
        px = price
        if px is None:
            try:
                px = float(CLIENT.get_price(s).get("lastPrice"))
            except (TypeError, ValueError):
                return _fail("INVALID_PARAM", "현재가 조회 실패 — price를 지정하세요")
        holdings = CLIENT.get_holdings()
        cash = None
        try:
            cash = float(CLIENT.get_buying_power("KRW").get("availableAmount"))
        except (TypeError, ValueError, TossApiError):
            cash = None
        return _wrap(analytics.impact_of_order(
            holdings, s, side, float(quantity), float(px), cur,
            buying_power_krw=cash, usdkrw=_usdkrw_rate(),
            symbol_name=sym.get("name")))
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


# ----- PLAN (dry-run; never POST) -----------------------------------------

def _usdkrw_rate_or_none() -> Optional[float]:
    """Real USD->KRW rate, or None if it cannot be fetched (no silent fallback).

    The execute gate needs to KNOW when the rate is unavailable so it can refuse
    a live USD order rather than price it off a guessed rate (invariant 12)."""
    try:
        r = float(CLIENT.get_exchange_rate("USD", "KRW").get("rate"))
        return r if r > 0 else None
    except Exception:  # noqa: BLE001
        return None


def _usdkrw_rate() -> float:
    """Fetch the current USD->KRW display rate (mock or live). Fail-safe 1380."""
    r = _usdkrw_rate_or_none()
    return r if r is not None else 1380.0


def _validate_order(symbol: str, side: str, order_type: str,
                    quantity: Optional[float], order_amount: Optional[float],
                    price: Optional[float], time_in_force: str,
                    account_index: int) -> dict:
    """Shared PLAN validation. Returns a frozen snapshot dict or raises."""
    side = side.upper()
    order_type = order_type.upper()
    if side not in {"BUY", "SELL"}:
        raise TossApiError("bad-side", "side must be BUY or SELL")
    if order_type not in {"LIMIT", "MARKET"}:
        raise TossApiError("bad-order-type", "order_type must be LIMIT or MARKET")
    if order_type == "LIMIT" and price is None:
        raise TossApiError("price-required", "LIMIT order requires price")

    currency = CLIENT.currency_for(symbol)
    if order_amount is not None and currency != "USD":
        raise TossApiError("amount-not-supported",
                           "order_amount(금액기반)은 미국주식(USD)만 지원")
    if quantity is None and order_amount is None:
        raise TossApiError("size-required", "quantity 또는 order_amount 필요")
    # Positivity — a zero/negative size or price must never reach a POST body.
    if quantity is not None and float(quantity) <= 0:
        raise TossApiError("invalid-quantity", "quantity는 0보다 커야 합니다")
    if order_amount is not None and float(order_amount) <= 0:
        raise TossApiError("invalid-amount", "order_amount는 0보다 커야 합니다")
    if price is not None and float(price) <= 0:
        raise TossApiError("invalid-price", "price는 0보다 커야 합니다")

    # Tick-size / price-band sanity (LIMIT only).
    notes = []
    if order_type == "LIMIT" and price is not None:
        tick = CLIENT.tick_size(symbol, price)
        if currency == "KRW" and (price % tick) != 0:
            raise TossApiError(
                "tick-violation",
                f"가격 {price}이(가) 호가단위 {tick}의 배수가 아님",
                data={"tickSize": tick})
        limits = CLIENT.get_price_limits(symbol)
        try:
            up = float(limits.get("upperLimitPrice"))
            low = float(limits.get("lowerLimitPrice"))
            if not (low <= price <= up):
                raise TossApiError(
                    "price-out-of-band",
                    f"가격 {price}이(가) 당일 {low}~{up} 밖",
                    data={"upperLimitPrice": up, "lowerLimitPrice": low})
        except (TypeError, ValueError):
            pass

    # Balance / sellable checks.
    if side == "BUY":
        bp = CLIENT.get_buying_power(currency)
        avail = float(bp.get("availableAmount", 0) or 0)
        est = (price or float(CLIENT.get_price(symbol)["lastPrice"])) * (quantity or 0)
        if order_amount is not None:
            est = order_amount
        if est > avail:
            raise TossApiError("insufficient-buying-power",
                               f"추정 체결액 {est} > 매수가능 {avail}",
                               data={"availableAmount": avail})
    else:
        # sellableQuantity is a STRING in live, a number in mock — coerce both.
        sq = float(CLIENT.get_sellable_quantity(symbol).get("sellableQuantity", 0) or 0)
        if quantity is not None and float(quantity) > sq:
            raise TossApiError("insufficient-sellable",
                               f"매도수량 {quantity} > 매도가능 {sq}",
                               data={"sellableQuantity": sq})

    # US fractional truncation simulation (amount-based).
    if currency == "USD" and quantity is not None:
        quantity = round(float(quantity), 6)
        if quantity <= 0:
            raise TossApiError("invalid-quantity",
                               "수량이 너무 작아 0으로 반올림됩니다(최소 0.000001주)")

    # Plan-time market price — frozen into the snapshot so the execute gate can
    # reject a stale preview when the live price has drifted (invariant 13B).
    market_price = None
    try:
        market_price = float(CLIENT.get_price(symbol)["lastPrice"])
    except (TypeError, ValueError, KeyError):
        market_price = None
    est_price = price if price is not None else market_price
    if est_price is None:
        raise TossApiError("price-unavailable",
                           f"{symbol} 현재가 조회 실패 — 잠시 후 다시 시도하세요")
    est_amount = order_amount if order_amount is not None else est_price * (quantity or 0)
    high_value = est_amount >= (100_000_000 if currency == "KRW" else 75_000)

    # Surface 거래제한/경고 in the dry-run plan (invariant 4 ext). The hard block
    # is *enforced* at execute (gate); here we only inform so the user sees it
    # before confirming. One get_warnings + one get_stocks call.
    raw_warnings = CLIENT.get_warnings(symbol).get("warnings", [])
    korean_detail = None
    try:
        srows = CLIENT.get_stocks([symbol]).get("stocks", []) or []
        if srows:
            korean_detail = srows[0].get("koreanMarketDetail")
    except Exception:  # noqa: BLE001
        korean_detail = None
    restrict = safety.classify_symbol_restrictions(raw_warnings, korean_detail)

    snapshot = {
        "symbol": symbol, "side": side, "orderType": order_type,
        "quantity": quantity, "orderAmount": order_amount, "price": price,
        "timeInForce": (time_in_force or "DAY").upper(),
        "currency": currency, "estimatedAmount": est_amount,
        "snapshotPrice": market_price,
        "highValue": high_value, "account_index": account_index,
        "warnings": raw_warnings,
        "restrictedBlock": restrict["block"],
        "restrictionWarnings": restrict["warn"],
        "wouldBlock": restrict["blocked"],
        "notes": notes,
    }
    return snapshot


def t_plan_order(symbol: str, side: str, order_type: str,
                 quantity: Optional[float] = None,
                 order_amount: Optional[float] = None,
                 price: Optional[float] = None, time_in_force: str = "DAY",
                 account_index: int = 0) -> dict:
    """[PLAN] 단일 주문 dry-run. 주문 미생성. preview_token + confirm_phrase 발급."""
    try:
        snap = _validate_order(symbol, side, order_type, quantity, order_amount,
                               price, time_in_force, account_index)
        token = PREVIEWS.put(snap, ttl_seconds=_clamp_ttl())
        full = PREVIEWS.peek(token) or snap
        return {
            "preview_token": token, "dryRun": True, "snapshot": full,
            "confirm_phrase": full.get("confirm_phrase"),
            "highValue": snap["highValue"],
            "note": ("실행하려면 place_order_confirmed(preview_token, confirm_phrase) 호출. "
                     "이 토큰은 1회용·TTL 만료됨."),
            "_mode": CFG.mode,
        }
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_plan_split_order(symbol: str, side: str, total_budget: float,
                       budget_currency: str, slices: int, order_type: str,
                       price: Optional[float] = None,
                       account_index: int = 0) -> dict:
    """[PLAN] 'N분할 매수' 자연어 분할 → N슬라이스 검증 후 배치 preview_token."""
    try:
        cands = CLIENT.resolve_symbol(symbol)
        if len(cands) != 1:
            return {"ambiguous": True, "candidates": cands, "_mode": CFG.mode}
        sym = cands[0]["symbol"]
        currency = cands[0]["currency"]
        slices = max(1, min(int(slices), 20))

        budget = float(total_budget)
        fx_note = None
        ttl = 120.0
        if budget_currency != currency:
            fx = CLIENT.get_exchange_rate(budget_currency, currency)
            rate = float(fx.get("rate", 1))
            budget = budget * rate
            fx_note = (f"{total_budget}{budget_currency} → {round(budget,2)}{currency} "
                       f"@{rate} (표시환율, 체결가 변동 가능)")
        per_slice = budget / slices

        plans = []
        for i in range(slices):
            last = float(CLIENT.get_price(sym)["lastPrice"])
            use_price = price if order_type.upper() == "LIMIT" else None
            if currency == "USD":
                sub = _validate_order(sym, side, order_type, None, per_slice,
                                      use_price, "DAY", account_index)
            else:
                qty = int(per_slice // (use_price or last)) if (use_price or last) else 0
                if qty < 1:
                    raise TossApiError("slice-too-small",
                                       f"슬라이스 예산 {per_slice}로 1주도 못 삼")
                sub = _validate_order(sym, side, order_type, qty, None,
                                      use_price, "DAY", account_index)
            sub["slice_index"] = i
            plans.append(sub)

        total_est = sum(p["estimatedAmount"] for p in plans)
        high_value = total_est >= (100_000_000 if currency == "KRW" else 75_000)
        batch = {
            "batch": True, "symbol": sym, "side": side.upper(), "slices": slices,
            "currency": currency, "estimatedAmount": total_est,
            "highValue": high_value, "plans": plans, "fxNote": fx_note,
            "account_index": account_index,
        }
        token = PREVIEWS.put(batch, ttl_seconds=ttl)
        full = PREVIEWS.peek(token) or batch
        return {"preview_token": token, "dryRun": True, "batch": full,
                "confirm_phrase": full.get("confirm_phrase"),
                "highValue": high_value, "_mode": CFG.mode}
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_plan_cancel_all(symbol: Optional[str] = None,
                      account_index: int = 0) -> dict:
    """[PLAN] 미체결(OPEN) 일괄취소 미리보기. 취소 미실행."""
    try:
        orders = CLIENT.list_orders("OPEN").get("orders", [])
        if symbol:
            orders = [o for o in orders if o.get("symbol") == symbol]
        targets = [{"orderId": o.get("orderId"), "symbol": o.get("symbol"),
                    "status": o.get("status")} for o in orders]
        snap = {"batch": True, "cancelAll": True, "symbol": symbol or "ALL",
                "side": "CANCEL", "slices": len(targets), "targets": targets,
                "account_index": account_index}
        token = PREVIEWS.put(snap, ttl_seconds=120.0)
        full = PREVIEWS.peek(token) or snap
        return {"preview_token": token, "dryRun": True, "targets": targets,
                "confirm_phrase": full.get("confirm_phrase"), "_mode": CFG.mode}
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_plan_cancel(order_id: str, account_index: int = 0) -> dict:
    """[PLAN] 단일 미체결 주문 취소 미리보기. 취소 미실행. preview_token 발급.

    cancel_order_confirmed(preview_token, confirm_phrase)로만 실제 취소된다.
    이미 종료/처리중(전량체결·취소완료·취소대기 등) 주문은 여기서 차단된다.
    """
    try:
        cur = CLIENT.get_order(order_id=order_id)
        status = (cur or {}).get("status")
        if not status:
            return {"error": {"code": "order-not-found",
                              "message": f"주문 {order_id}을(를) 찾을 수 없습니다"},
                    "_mode": CFG.mode}
        if status in _ORDER_TERMINAL:
            return _fail("INVALID_PARAM",
                         f"취소 불가: 주문이 이미 종료 상태입니다"
                         f"({ORDER_STATUS_KO.get(status, status)}).",
                         order_id=order_id, status=status)
        if status in _ORDER_IN_TRANSITION:
            return _fail("INVALID_PARAM",
                         f"취소 불가: 주문이 이미 {ORDER_STATUS_KO.get(status, status)} "
                         "상태입니다(처리 중).", order_id=order_id, status=status)
        snap = {"orderId": order_id, "symbol": cur.get("symbol"),
                "side": "CANCEL", "cancelAll": False,
                "currentStatus": status, "account_index": account_index}
        token = PREVIEWS.put(snap, ttl_seconds=120.0)
        full = PREVIEWS.peek(token) or snap
        return {"preview_token": token, "dryRun": True, "orderId": order_id,
                "symbol": cur.get("symbol"), "currentStatus": status,
                "currentStatusKo": ORDER_STATUS_KO.get(status, status),
                "confirm_phrase": full.get("confirm_phrase"), "_mode": CFG.mode}
    except Exception as e:  # noqa: BLE001
        return _err(e)


def t_logout() -> dict:
    """[READ] 캐시된 OAuth 토큰을 폐기(메모리 + token.json 삭제). 다음 호출 시 재발급.

    secret은 건드리지 않는다(.env에 그대로). 토큰만 무효화한다.
    """
    try:
        out = CLIENT.logout()
        return out
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ----- PLAN (calculation-only; no preview_token, never POST) ---------------

def t_plan_dca(symbol: str, period_amount: float, periods: int,
               budget_currency: str = "KRW", interval: str = "1d") -> dict:
    """[PLAN/계산] 적립식(정액) DCA 스케줄 시뮬. '월 50만원 적립하면?'

    실행 아님. 과거 캔들 종가로 회차별 매수 수량·누적 평단을 시뮬레이션한다.
    period_amount: 회차당 투자금(budget_currency). periods: 회차 수(<=120).
    """
    try:
        sym, fail = _resolve_one(symbol)
        if fail:
            return fail
        s = sym["symbol"]
        currency = (sym.get("currency") or CLIENT.currency_for(s)).upper()
        periods = max(1, min(int(periods), 120))
        amt = float(period_amount)
        if amt <= 0:
            return _fail("INVALID_PARAM", "period_amount는 0보다 커야 합니다")

        # Currency/market coherence (invariant 12).
        cm = safety.check_currency_match(s, budget_currency.upper(),
                                         symbol_currency=currency)
        if cm:
            return cm

        usdkrw = _usdkrw_rate()
        per_amt = amt  # in budget_currency; assume budget_currency == market ccy
        # Use historical closes spaced across the available window as fill prices.
        candles = CLIENT.get_candles(s, interval=interval,
                                     count=min(200, max(periods, 30)))
        rows = candles.get("candles", [])
        closes = [c.get("close") for c in rows if c.get("close") is not None]
        if not closes:
            return _fail("INVALID_PARAM", "캔들 데이터 부족")
        # Sample `periods` price points evenly across the window (oldest->newest).
        n = len(closes)
        idxs = [round(i * (n - 1) / max(1, periods - 1)) for i in range(periods)]
        schedule, total_qty, total_cost = [], 0.0, 0.0
        for i, ix in enumerate(idxs):
            px = float(closes[ix])
            if currency == "USD":
                qty = round(per_amt / px, 6) if px else 0.0
            else:
                qty = float(int(per_amt // px)) if px else 0.0
            spent = qty * px
            total_qty += qty
            total_cost += spent
            schedule.append({
                "period": i + 1, "price": round(px, 2),
                "quantity": qty, "spent": round(spent, 2),
            })
        avg_cost = (total_cost / total_qty) if total_qty else None
        return _ok({
            "symbol": s, "name": sym.get("name"), "currency": currency,
            "periods": periods, "periodAmount": amt,
            "schedule": schedule,
            "totalQuantity": round(total_qty, 6),
            "totalCost": round(total_cost, 2),
            "averageCost": round(avg_cost, 2) if avg_cost else None,
            "usdkrw": usdkrw if currency == "USD" else None,
            "executed": False,
            "note": "적립식 시뮬레이션 — 과거 종가 기준, 실제 체결가 아님. 실행 없음.",
            "_disclaimer": "참고용 플랜 — 매매 추천 아님.",
        })
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


def t_plan_trailing_stop(symbol: str, entry_price: Optional[float] = None,
                         trail_pct: float = 5.0,
                         quantity: Optional[float] = None) -> dict:
    """[PLAN/계산] 트레일링스탑 플랜: 진입가·추적%·현재 트리거가·여유폭.

    실행 아님(자동 추적은 후속 분리). entry_price 미지정 시 현재가 사용.
    trail_pct: 고점 대비 하락 트리거 비율(%).
    """
    try:
        sym, fail = _resolve_one(symbol)
        if fail:
            return fail
        s = sym["symbol"]
        currency = (sym.get("currency") or CLIENT.currency_for(s)).upper()
        trail = float(trail_pct)
        if not (0 < trail < 100):
            return _fail("INVALID_PARAM", "trail_pct는 0~100 사이여야 합니다")
        price = CLIENT.get_price(s)
        current = None
        try:
            current = float(price.get("lastPrice"))
        except (TypeError, ValueError):
            return _fail("INVALID_PARAM", "현재가 조회 실패")
        entry = float(entry_price) if entry_price is not None else current
        # Highwater = max(entry, current) as the running peak this far.
        highwater = max(entry, current)
        trigger = highwater * (1 - trail / 100.0)
        room_pct = (current - trigger) / current * 100.0 if current else None
        unrealized_pct = (current - entry) / entry * 100.0 if entry else None
        # Round trigger to a plausible tick for KRX.
        if currency == "KRW":
            tick = CLIENT.tick_size(s, trigger)
            trigger = float(int(trigger // tick * tick)) if tick else trigger
        return _ok({
            "symbol": s, "name": sym.get("name"), "currency": currency,
            "entryPrice": round(entry, 2), "currentPrice": round(current, 2),
            "highwater": round(highwater, 2), "trailPct": trail,
            "triggerPrice": round(trigger, 2),
            "roomToTriggerPct": round(room_pct, 2) if room_pct is not None else None,
            "unrealizedPct": round(unrealized_pct, 2) if unrealized_pct is not None else None,
            "quantity": quantity,
            "executed": False,
            "note": "트레일링스탑 플랜 — 자동 추적/실행 없음. 트리거 도달 시 plan_order로 수동 진행.",
            "_disclaimer": "참고용 플랜 — 매매 추천 아님.",
        })
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


def t_plan_rebalance(target_weights: str, account_index: int = 0,
                     total_budget_krw: Optional[float] = None) -> dict:
    """[PLAN/계산] 목표 비중 대비 매수/매도 수량 플랜. 실행은 plan_order로 분리.

    target_weights: "심볼:비중" 콤마구분(예: "005930:0.4,NVDA:0.3,AAPL:0.3").
    비중 합은 1.0 근처여야 한다. 현재 보유 평가액 대비 차이를 수량으로 환산.
    """
    try:
        pairs = []
        for tok in target_weights.split(","):
            tok = tok.strip()
            if not tok or ":" not in tok:
                continue
            k, _, v = tok.partition(":")
            try:
                pairs.append((k.strip(), float(v)))
            except ValueError:
                return _fail("INVALID_PARAM", f"비중 파싱 실패: '{tok}'")
        if not pairs:
            return _fail("INVALID_PARAM",
                         "target_weights 형식: '심볼:비중,심볼:비중'")
        wsum = sum(w for _, w in pairs)
        if not (0.95 <= wsum <= 1.05):
            return _fail("INVALID_PARAM",
                         f"목표 비중 합이 1.0이 아닙니다(현재 {round(wsum,3)})")

        usdkrw = _usdkrw_rate()
        holdings = CLIENT.get_holdings()
        rows = holdings.get("holdings", []) or []
        # current KRW eval per symbol
        cur_eval_krw, cur_px = {}, {}
        total_eq = 0.0
        for r in rows:
            cur = (r.get("currency") or "").upper()
            ev = float(r.get("evaluationAmount") or 0)
            krw = ev * usdkrw if cur == "USD" else ev
            cur_eval_krw[r.get("symbol")] = krw
            total_eq += krw
        base = float(total_budget_krw) if total_budget_krw else total_eq
        if base <= 0:
            return _fail("INVALID_PARAM",
                         "재배분 기준 금액이 0입니다(보유 없음·total_budget_krw 미지정)")

        actions = []
        for q, w in pairs:
            sym, fail = _resolve_one(q)
            if fail:
                # keep going but flag the symbol
                actions.append({"symbol": q, "error": "해석 실패"})
                continue
            s = sym["symbol"]
            currency = (sym.get("currency") or CLIENT.currency_for(s)).upper()
            try:
                px = float(CLIENT.get_price(s).get("lastPrice"))
            except (TypeError, ValueError):
                actions.append({"symbol": s, "error": "현재가 조회 실패"})
                continue
            px_krw = px * usdkrw if currency == "USD" else px
            target_krw = base * w
            cur_krw = cur_eval_krw.get(s, 0.0)
            diff_krw = target_krw - cur_krw
            side = "BUY" if diff_krw > 0 else ("SELL" if diff_krw < 0 else "HOLD")
            if currency == "USD":
                qty = round(abs(diff_krw) / px_krw, 6) if px_krw else 0.0
            else:
                qty = float(int(abs(diff_krw) // px_krw)) if px_krw else 0.0
            actions.append({
                "symbol": s, "name": sym.get("name"), "currency": currency,
                "targetWeight": w, "currentKRW": round(cur_krw),
                "targetKRW": round(target_krw), "diffKRW": round(diff_krw),
                "side": side, "price": round(px, 2), "quantity": qty,
            })
        return _ok({
            "baseKRW": round(base), "totalEquityKRW": round(total_eq),
            "usdkrw": usdkrw, "actions": actions, "executed": False,
            "note": "리밸런싱 플랜 — 실행 없음. 각 액션은 plan_order로 미리보기 후 진행.",
            "_disclaimer": "참고용 플랜 — 매매 추천 아님.",
        })
    except Exception as e:  # noqa: BLE001
        return _err_envelope(e)


# ----- EXECUTE (mutates; only registered when allow_live_orders) -----------

def _symbol_restriction_state(symbol: str) -> dict:
    """Fetch a symbol's warnings + koreanMarketDetail ONCE and classify them.

    Single call each to get_warnings + get_stocks (rate-floods avoided — one
    invocation per gate; both mock and live). Returns the classifier dict
    ``{block, warn, blocked}`` (empty on any fetch failure -> fail-open to the
    warning surface, never silently allows a HARD halt we *did* observe).
    """
    warnings = []
    korean_detail = None
    try:
        warnings = CLIENT.get_warnings(symbol).get("warnings", []) or []
    except Exception:  # noqa: BLE001
        warnings = []
    try:
        rows = CLIENT.get_stocks([symbol]).get("stocks", []) or []
        if rows:
            korean_detail = rows[0].get("koreanMarketDetail")
    except Exception:  # noqa: BLE001
        korean_detail = None
    return safety.classify_symbol_restrictions(warnings, korean_detail)


def _order_safety_gate(snap: dict, tool: str) -> Optional[dict]:
    """Apply safety invariants 7-12 immediately before an order POST.

    Runs ON TOP OF the existing 4-gate (live + preview_token + confirm_phrase +
    kill) — never weakens them. Order of checks:
      9  circuit/kill  -> 12 currency (fail-closed) -> 4(ext) restricted-symbol ->
      8 limits(KRW notional; estimated-FX live order refused) ->
      11 market-open (live only; calendar injected) ->
      13B price-drift (live) -> 7 duplicate.
    Returns ``None`` to proceed, else a {ok:false,error} envelope to return.
    Cautionary warnings (단기과열/투자경고/VI) do NOT block — they are stamped
    onto ``snap['_gateWarnings']`` so the caller can surface them.
    The audit log (invariant 10) is written by the caller around this gate.
    """
    symbol = snap.get("symbol")
    side = snap.get("side")
    order_type = snap.get("orderType")
    currency = (snap.get("currency") or "").upper()
    coid = snap.get("clientOrderId")

    # 9 — circuit breaker + file kill-switch (single pre-POST gate).
    if (err := GUARD.check_circuit()) is not None:
        return err

    # 12 — currency / market coherence. Currency is REQUIRED; a missing/unknown
    # currency is a HARD fail (fail-closed — never skip the limit/match checks).
    if symbol:
        if currency not in {"KRW", "USD"}:
            return safety._err(
                safety.ERR_INVALID,
                f"주문 통화가 불명확합니다(currency={currency!r}) — "
                "KRW 또는 USD가 필요합니다. preview_order를 다시 실행하세요.")
        sym_ccy = None
        try:
            sym_ccy = CLIENT.currency_for(symbol)
        except Exception:  # noqa: BLE001
            sym_ccy = None
        if (err := safety.check_currency_match(
                symbol, currency, symbol_currency=sym_ccy)) is not None:
            return err

    # 4 (ext) — restricted symbol (정리매매/투자위험/거래정지 -> BLOCK; cautionary
    # states -> stamp warnings onto the snapshot, do NOT block). One fetch each.
    if symbol:
        cls = _symbol_restriction_state(symbol)
        if cls.get("warn"):
            snap["_gateWarnings"] = cls["warn"]
        if cls.get("blocked"):
            return safety._err(
                safety.ERR_RESTRICTED,
                "거래제한 종목입니다(주문 차단): " + ", ".join(cls["block"]) +
                ". 정리매매·투자위험·거래정지 종목은 실주문이 거부됩니다.",
                block=cls["block"], warn=cls.get("warn", []))

    # 8 — per-order + daily KRW notional + count ceilings. Fail-closed: with a
    # currency guaranteed above, an estimatedAmount always reaches the ceiling.
    est = snap.get("estimatedAmount")
    if est is not None:
        usd_rate = None
        if currency == "USD":
            usd_rate = _usdkrw_rate_or_none()
            # invariant 12: never place a LIVE USD order without a real FX rate.
            if CFG.live and usd_rate is None:
                return safety._err(
                    safety.ERR_CURRENCY,
                    "USD 환율을 확인할 수 없어 실주문을 거부합니다 — "
                    "잠시 후 다시 시도하세요.", rate_source="unavailable")
            if usd_rate is None:           # mock: conservative fallback for the cap
                usd_rate = _usdkrw_rate()
        krw_notional, _meta = safety.to_krw_notional(
            float(est), currency, usd_krw_rate=usd_rate)
        if (err := GUARD.check_limits(krw_notional)) is not None:
            return err

    # 11 — market open (live only; mock skips so demos run any time).
    if CFG.live and symbol:
        region = CLIENT.region_for(symbol) if hasattr(CLIENT, "region_for") else (
            "KR" if (symbol or "").isdigit() else "US")
        try:
            calendar = CLIENT.get_market_calendar(region)
        except Exception:  # noqa: BLE001
            calendar = None
        # 백스톱: 캘린더가 '휴장'이라 해도 최근 실체결이 있으면 시장은 실제 개장 상태
        # (토스 US 캘린더 isHoliday 오작동 교정 — 앱은 되는데 API만 막히던 원인). 체결
        # 없으면 그대로 차단(fail-safe). _market_live_by_trades 주석 참조.
        # 백스톱은 sessions를 비워 시간검사를 통째로 스킵시키므로, '실제 정규장 시간'이거나
        # 의도된 장외 예외진입(--buy-ext)일 때만 적용한다(감사#232: 장외에 캘린더가 오작동하면
        # MCP 주문이 정규장 제한 없이 통과하던 구멍).
        if calendar and (calendar.get("isHoliday") is True
                         or calendar.get("isOpen") is False):
            if _market_live_by_trades(symbol) and (
                    _et_regular_now() or bool(globals().get("ALLOW_EXTENDED_ORDER"))):
                calendar = {**calendar, "isHoliday": False,
                            "isOpen": True, "sessions": []}
        # 장외 예외진입(--buy-ext)만 확장세션 허용. 전략 실행기가 in-process로 이 플래그를
        # 세팅했을 때만 True — MCP는 별도 프로세스라 항상 False(정규장 강제 유지). 감사#223.
        if (err := safety.check_market_open(
                calendar, allow_extended=bool(globals().get("ALLOW_EXTENDED_ORDER")))) is not None:
            return err

    # 13B — price drift: refuse a stale preview if the live price has moved
    # beyond tolerance since the plan was frozen. Applies to orders whose fill
    # price tracks the market (MARKET, or amount-based with no explicit price);
    # an explicit LIMIT price is already bounded, so we don't over-block it.
    tracks_market = (order_type == "MARKET") or (snap.get("price") is None)
    if CFG.live and symbol and tracks_market:
        try:
            current = float(CLIENT.get_price(symbol)["lastPrice"])
        except (TypeError, ValueError, KeyError):
            current = None
        if current is None:
            # Can't verify the live price -> fail CLOSED (never fill blind).
            return safety._err(
                safety.ERR_PRICE_MOVED,
                "현재가를 확인할 수 없어 실주문을 보류합니다 — "
                "preview_order를 다시 실행하세요.")
        if (err := safety.check_price_drift(
                snap.get("snapshotPrice"), current)) is not None:
            return err

    # 7 — idempotency / duplicate within the dedup window.
    if coid and (err := GUARD.check_duplicate(coid)) is not None:
        return err

    return None


# 장외 예외진입 게이트(감사#223): 전략 실행기(execute.py --buy-ext)가 in-process로만
# True로 세팅한다. MCP 서버 프로세스에서는 항상 False → MCP 주문은 정규장 강제 유지.
ALLOW_EXTENDED_ORDER = False


def _notify_mcp_order(snap: dict, oid) -> None:
    """B4: 전략 실행기 밖(MCP/수동) 경로의 주문 성공을 alerts.jsonl에 남김 —
    전략 원장과 무관한 주문이 '조용히' 발생하는 것을 가시화(verify-broker 역드리프트와 짝).
    execute 자신(TOSS_EXECUTE_LOCK_HELD=1)의 주문은 저널이 기록하므로 제외. 실패 조용히."""
    import os as _os
    if _os.environ.get("TOSS_EXECUTE_LOCK_HELD") == "1":
        return
    try:
        from datetime import datetime as _dt
        p = _os.path.expanduser("~/.toss-trader/alerts.jsonl")
        rec = {"ts": _dt.now().isoformat(timespec="seconds"), "kind": "MCP_ORDER",
               "msg": f"{snap.get('side')} {snap.get('symbol')} "
                      f"qty={snap.get('quantity')} amt={snap.get('estimatedAmount')} oid={oid}"}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _execute_lock_gate() -> Optional[dict]:
    """MH8/T9: 전략 실행기(execute.py)가 단일실행 락을 쥔 채 주문 진행중이면 MCP 경로
    주문을 거부(교차 이중매수/coid 충돌 방지). execute.py 자신은 락 획득 후
    TOSS_EXECUTE_LOCK_HELD=1을 세팅하고 in-process 호출하므로 통과.
    게이트 자체의 예외는 주문을 막지 않음(기존 안전게이트들이 방어) — 락 경합만 차단."""
    import os as _os
    if _os.environ.get("TOSS_EXECUTE_LOCK_HELD") == "1":
        return None
    try:
        import fcntl
        lp = _os.path.expanduser("~/.toss-trader/execute.lock")
        if not _os.path.exists(lp):
            return None
        f = open(lp, "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return None
        except (BlockingIOError, OSError):
            return {"error": {"code": "execute-running",
                              "message": "전략 실행기(execute.py)가 주문 진행중 — "
                                         "교차 이중주문 방지를 위해 잠시 후 재시도"}}
        finally:
            f.close()
    except Exception:
        return None


def t_place_order_confirmed(preview_token: str, confirm_phrase: str,
                            confirm_high_value: bool = False) -> dict:
    """[EXECUTE] 실주문 생성. preview_token + confirm_phrase로만 동작.

    안전 불변식 7(멱등)·8(한도)·9(회로/kill)·10(감사)·11(개장)·12(통화)가
    기존 4중 게이트(live+preview_token+confirm_phrase+kill) 위에 중첩 적용된다.
    """
    snap = None
    reserved = False   # H-NEW-1: release only OUR OWN reservation on failure
    try:
        snap = PREVIEWS.consume(preview_token, confirm_phrase)
        if snap.get("batch"):
            raise TossApiError("wrong-tool",
                               "배치 토큰입니다 — cancel_all_confirmed 사용")
        if snap.get("highValue") and not confirm_high_value:
            raise TossApiError("confirm-high-value-required",
                               "1억↑/고액 주문은 confirm_high_value=True 필요")
        # MH8/T9: 전략 실행기 락 게이트(교차 이중주문 방지) — 다른 게이트보다 먼저
        lg = _execute_lock_gate()
        if lg is not None:
            return lg
        # Invariants 7-12 (on top of the 4-gate). Audit the attempt either way.
        gate = _order_safety_gate(snap, "place_order_confirmed")
        if gate is not None:
            GUARD.audit(mode=("live" if CFG.live else "dry"),
                        tool="place_order_confirmed", symbol=snap.get("symbol"),
                        side=snap.get("side"), quantity=snap.get("quantity"),
                        price=snap.get("price"),
                        est_amount=snap.get("estimatedAmount"),
                        currency=snap.get("currency"),
                        client_order_id=snap.get("clientOrderId"),
                        confirm_present=True, preview_token_present=True,
                        error=gate)
            return gate
        # invariant 7 (TOCTOU): atomically CLAIM the clientOrderId before the
        # POST — two concurrent identical submits can't both pass the gate's
        # read-only duplicate check.
        if (dup := GUARD.reserve_submission(snap.get("clientOrderId"))) is not None:
            GUARD.audit(mode=("live" if CFG.live else "dry"),
                        tool="place_order_confirmed", symbol=snap.get("symbol"),
                        side=snap.get("side"),
                        client_order_id=snap.get("clientOrderId"),
                        confirm_present=True, preview_token_present=True,
                        error=dup)
            return dup
        reserved = True
        # high-value -> Toss confirmHighValueOrder flag.
        if snap.get("highValue"):
            snap = dict(snap)
            snap["confirmHighValueOrder"] = True
        gate_warnings = snap.get("_gateWarnings") or []
        result = CLIENT.place_order(snap)
        result["_mode"] = CFG.mode
        if gate_warnings:
            # Cautionary warnings (단기과열/투자경고/VI) surfaced, NOT a block.
            result["_warnings"] = gate_warnings
        oid = result.get("orderId")
        GUARD.confirm_submission(snap.get("clientOrderId"), oid)
        _notify_mcp_order(snap, oid)   # B4: 비-execute(MCP/수동) 주문 out-of-band 알림
        krw, _m = safety.to_krw_notional(
            float(snap.get("estimatedAmount") or 0),
            (snap.get("currency") or "KRW"),
            usd_krw_rate=(_usdkrw_rate() if snap.get("currency") == "USD" else None))
        GUARD.record_order_result(success=True, krw_notional=krw, count=1)
        audit_extra = {"gate_warnings": gate_warnings} if gate_warnings else {}
        GUARD.audit(mode=("live" if CFG.live else "dry"),
                    tool="place_order_confirmed", symbol=snap.get("symbol"),
                    side=snap.get("side"), quantity=snap.get("quantity"),
                    price=snap.get("price"),
                    est_amount=snap.get("estimatedAmount"),
                    currency=snap.get("currency"),
                    client_order_id=snap.get("clientOrderId"), order_id=oid,
                    confirm_present=True, preview_token_present=True,
                    **audit_extra)
        return result
    except PermissionError as e:
        return {"error": {"code": "confirm-mismatch", "message": str(e)},
                "_mode": CFG.mode}
    except KeyError as e:
        return {"error": {"code": "preview-invalid", "message": str(e)},
                "_mode": CFG.mode}
    except Exception as e:  # noqa: BLE001
        GUARD.record_order_result(success=False)
        if snap is not None:
            # DEFINITE failure AFTER our own reserve -> free the reservation so
            # a corrected retry isn't dedup-blocked. `reserved` guard (H-NEW-1):
            # a pre-reserve failure (wrong-tool / confirm-high-value-required)
            # must NOT release a concurrent call's in-flight claim on the same
            # intent. UNCERTAIN outcome (reconcile-required) keeps the claim.
            if reserved and getattr(e, "code", None) != "reconcile-required":
                GUARD.release_submission(snap.get("clientOrderId"))
            GUARD.audit(mode=("live" if CFG.live else "dry"),
                        tool="place_order_confirmed", symbol=snap.get("symbol"),
                        side=snap.get("side"),
                        client_order_id=snap.get("clientOrderId"),
                        confirm_present=True, preview_token_present=True,
                        error=str(e))
        return _err(e)


def t_modify_order_confirmed(preview_token: str, confirm_phrase: str,
                             confirm_high_value: bool = False) -> dict:
    """[EXECUTE] 미체결 주문 정정. 새 orderId 반환 → 추적 교체.

    안전 불변식 9(회로/kill)·12(통화)·11(개장)·10(감사)가 4중 게이트에 중첩.
    """
    snap = None
    try:
        snap = PREVIEWS.consume(preview_token, confirm_phrase)
        order_id = snap.get("orderId")
        if not order_id:
            raise TossApiError("no-order-id", "정정 대상 orderId 미지정")
        # Invariant 6 — refuse modify on terminal/in-transition orders.
        if (state := _order_state_gate(order_id, "정정")) is not None:
            GUARD.audit(mode=("live" if CFG.live else "dry"),
                        tool="modify_order_confirmed", symbol=snap.get("symbol"),
                        order_id=order_id,
                        client_order_id=snap.get("clientOrderId"),
                        confirm_present=True, preview_token_present=True,
                        error=state)
            return state
        gate = _order_safety_gate(snap, "modify_order_confirmed")
        if gate is not None:
            GUARD.audit(mode=("live" if CFG.live else "dry"),
                        tool="modify_order_confirmed", symbol=snap.get("symbol"),
                        side=snap.get("side"), price=snap.get("price"),
                        currency=snap.get("currency"), order_id=order_id,
                        client_order_id=snap.get("clientOrderId"),
                        confirm_present=True, preview_token_present=True,
                        error=gate)
            return gate
        result = CLIENT.modify_order(order_id, snap)
        result["_mode"] = CFG.mode
        result["_note"] = "정정으로 새 orderId 발급됨 — track_order로 재조회 필요"
        GUARD.record_order_result(success=True, count=0)
        GUARD.audit(mode=("live" if CFG.live else "dry"),
                    tool="modify_order_confirmed", symbol=snap.get("symbol"),
                    side=snap.get("side"), price=snap.get("price"),
                    currency=snap.get("currency"), order_id=order_id,
                    client_order_id=snap.get("clientOrderId"),
                    confirm_present=True, preview_token_present=True)
        return result
    except PermissionError as e:
        return {"error": {"code": "confirm-mismatch", "message": str(e)},
                "_mode": CFG.mode}
    except KeyError as e:
        return {"error": {"code": "preview-invalid", "message": str(e)},
                "_mode": CFG.mode}
    except Exception as e:  # noqa: BLE001
        GUARD.record_order_result(success=False)
        if snap is not None:
            GUARD.audit(mode=("live" if CFG.live else "dry"),
                        tool="modify_order_confirmed", symbol=snap.get("symbol"),
                        order_id=snap.get("orderId"),
                        client_order_id=snap.get("clientOrderId"),
                        confirm_present=True, preview_token_present=True,
                        error=str(e))
        return _err(e)


def _order_state_gate(order_id: str, action_ko: str) -> Optional[dict]:
    """Invariant 6: refuse cancel/modify on a terminal or in-transition order.

    Fetches the order's current status via get_order; if it is FILLED/CANCELED/
    REJECTED/REPLACED (terminal) or already PENDING_CANCEL/PENDING_REPLACE
    (in transition), returns an INVALID_PARAM envelope so we never fire a
    pointless/double cancel or modify. A lookup failure does NOT block (we let
    the broker be the final authority) but a clearly-terminal status does.
    """
    if not order_id:
        return None
    try:
        cur = CLIENT.get_order(order_id=order_id)
    except Exception:  # noqa: BLE001
        return None  # lookup failed -> let the broker decide (don't false-block)
    status = (cur or {}).get("status")
    if not status:
        return None
    if status in _ORDER_TERMINAL:
        return _fail(
            "INVALID_PARAM",
            f"{action_ko} 불가: 주문이 이미 종료 상태입니다"
            f"({ORDER_STATUS_KO.get(status, status)}).",
            order_id=order_id, status=status)
    if status in _ORDER_IN_TRANSITION:
        return _fail(
            "INVALID_PARAM",
            f"{action_ko} 불가: 주문이 이미 {ORDER_STATUS_KO.get(status, status)} "
            "상태입니다(처리 중).",
            order_id=order_id, status=status)
    return None


def _cancel_circuit_gate(tool: str) -> Optional[dict]:
    """Cancel/cancel-all only need the circuit/kill gate (9) pre-POST.

    (No notional/currency for a cancel; market-open also applies live so a
    cancel during a closed session is refused unless the broker accepts it —
    we keep it strict for safety symmetry.)
    """
    if (err := GUARD.check_circuit()) is not None:
        return err
    return None


def t_cancel_order_confirmed(preview_token: str, confirm_phrase: str) -> dict:
    """[EXECUTE] 주문 취소. 새 orderId 반환 → 추적 교체.

    안전 불변식 9(회로/kill)·10(감사)가 4중 게이트에 중첩.
    """
    snap = None
    try:
        snap = PREVIEWS.consume(preview_token, confirm_phrase)
        order_id = snap.get("orderId")
        if not order_id:
            raise TossApiError("no-order-id", "취소 대상 orderId 미지정")
        # Invariant 6 — refuse cancel on terminal/in-transition orders.
        if (state := _order_state_gate(order_id, "취소")) is not None:
            GUARD.audit(mode=("live" if CFG.live else "dry"),
                        tool="cancel_order_confirmed", symbol=snap.get("symbol"),
                        order_id=order_id,
                        client_order_id=snap.get("clientOrderId"),
                        confirm_present=True, preview_token_present=True,
                        error=state)
            return state
        gate = _cancel_circuit_gate("cancel_order_confirmed")
        if gate is not None:
            GUARD.audit(mode=("live" if CFG.live else "dry"),
                        tool="cancel_order_confirmed", symbol=snap.get("symbol"),
                        order_id=order_id,
                        client_order_id=snap.get("clientOrderId"),
                        confirm_present=True, preview_token_present=True,
                        error=gate)
            return gate
        # Invariant 7 — idempotency for cancels, keyed on the target order_id
        # (NOT the intent-derived clientOrderId, which collides across orders of
        # the same symbol). Blocks a rapid double-cancel before the broker has
        # moved the order to PENDING_CANCEL.
        if (dup := GUARD.check_duplicate(f"cancel:{order_id}")) is not None:
            GUARD.audit(mode=("live" if CFG.live else "dry"),
                        tool="cancel_order_confirmed", symbol=snap.get("symbol"),
                        order_id=order_id, confirm_present=True,
                        preview_token_present=True, error=dup)
            return dup
        result = CLIENT.cancel_order(order_id,
                                     client_order_id=snap.get("clientOrderId"))
        result["_mode"] = CFG.mode
        GUARD.record_submission(f"cancel:{order_id}", result.get("orderId"))
        GUARD.record_order_result(success=True, count=0)
        GUARD.audit(mode=("live" if CFG.live else "dry"),
                    tool="cancel_order_confirmed", symbol=snap.get("symbol"),
                    order_id=order_id,
                    client_order_id=snap.get("clientOrderId"),
                    confirm_present=True, preview_token_present=True)
        return result
    except PermissionError as e:
        return {"error": {"code": "confirm-mismatch", "message": str(e)},
                "_mode": CFG.mode}
    except KeyError as e:
        return {"error": {"code": "preview-invalid", "message": str(e)},
                "_mode": CFG.mode}
    except Exception as e:  # noqa: BLE001
        GUARD.record_order_result(success=False)
        if snap is not None:
            GUARD.audit(mode=("live" if CFG.live else "dry"),
                        tool="cancel_order_confirmed", symbol=snap.get("symbol"),
                        order_id=snap.get("orderId"),
                        client_order_id=snap.get("clientOrderId"),
                        confirm_present=True, preview_token_present=True,
                        error=str(e))
        return _err(e)


def t_cancel_all_confirmed(preview_token: str, confirm_phrase: str) -> dict:
    """[EXECUTE] 미체결 일괄취소. 토큰 스냅샷 대상만 순차 취소.

    안전 불변식 9(회로/kill)·10(감사)가 4중 게이트에 중첩(취소 1건마다 감사).
    """
    snap = None
    try:
        snap = PREVIEWS.consume(preview_token, confirm_phrase)
        if not snap.get("cancelAll"):
            raise TossApiError("wrong-tool", "일괄취소 토큰이 아님")
        gate = _cancel_circuit_gate("cancel_all_confirmed")
        if gate is not None:
            GUARD.audit(mode=("live" if CFG.live else "dry"),
                        tool="cancel_all_confirmed",
                        symbol=snap.get("symbol"), confirm_present=True,
                        preview_token_present=True, error=gate)
            return gate
        results = []
        for t in snap.get("targets", []):
            oid = t.get("orderId")
            try:
                r = CLIENT.cancel_order(oid)
                results.append({"orderId": oid, "result": r})
                GUARD.audit(mode=("live" if CFG.live else "dry"),
                            tool="cancel_all_confirmed",
                            symbol=t.get("symbol"), order_id=oid,
                            confirm_present=True, preview_token_present=True)
            except Exception as e:  # noqa: BLE001 — partial failure allowed
                results.append({"orderId": oid, "error": str(e)})
                GUARD.audit(mode=("live" if CFG.live else "dry"),
                            tool="cancel_all_confirmed",
                            symbol=t.get("symbol"), order_id=oid,
                            confirm_present=True, preview_token_present=True,
                            error=str(e))
        GUARD.record_order_result(success=True, count=0)
        return {"canceled": results, "count": len(results), "_mode": CFG.mode}
    except PermissionError as e:
        return {"error": {"code": "confirm-mismatch", "message": str(e)},
                "_mode": CFG.mode}
    except KeyError as e:
        return {"error": {"code": "preview-invalid", "message": str(e)},
                "_mode": CFG.mode}
    except Exception as e:  # noqa: BLE001
        return _err(e)


# ===========================================================================
# Registration & entry point
# ===========================================================================

READ_TOOLS = [
    t_get_account_info, t_resolve_symbol, t_get_quote, t_get_prices,
    t_get_orderbook, t_get_candles, t_get_trades, t_get_price_limits,
    t_get_stock_warnings, t_stock_info, t_get_exchange_rate, t_get_market_calendar,
    t_get_holdings, t_get_buying_power, t_get_sellable_quantity,
    t_get_commission_policy, t_get_orders, t_track_order,
    t_portfolio_overview, t_analyze_symbol, t_logout,
    # ----- new alpha READ tools (analytics.py; common {ok,data,_mock}) -----
    t_orderbook_pressure, t_intraday_vwap, t_recent_trades_tape,
    t_price_limit_proximity, t_scalp_score, t_supply_demand_flow,
    t_rank_watchlist, t_portfolio_risk_xray, t_pnl_attribution,
    t_impact_of_order,
]
PLAN_TOOLS = [
    t_plan_order, t_plan_split_order, t_plan_cancel, t_plan_cancel_all,
    # ----- new calculation-only PLAN tools (no POST) -----
    t_plan_dca, t_plan_trailing_stop, t_plan_rebalance,
]
WRITE_TOOLS = [
    t_place_order_confirmed, t_modify_order_confirmed,
    t_cancel_order_confirmed, t_cancel_all_confirmed,
]

# Catalog-name aliases for existing tools (back-compat: original names kept).
# {alias_name: backing_function}. Registered in addition to the primary names.
ALIASES = {
    "portfolio_snapshot": t_portfolio_overview,   # 카탈로그명 ← portfolio_overview
    "plan_split_buy": t_plan_split_order,          # 카탈로그명 ← plan_split_order
    "price_quote": t_get_prices,                   # 카탈로그명 ← get_prices (다건 시세)
    "candles": t_get_candles,                      # 카탈로그명 ← get_candles
    "preview_order": t_plan_order,                 # 카탈로그명 ← plan_order (preview_token 발급)
    # analyze_symbol is already the public name of t_analyze_symbol (catalog-aligned).
}

# Write-tool catalog alias — registered ONLY when allow_live_orders (never exposed
# in read-only mode, preserving the "write tools hidden by default" invariant).
WRITE_ALIASES = {
    "place_order": t_place_order_confirmed,        # 카탈로그명 ← place_order_confirmed
}

# Public tool names (strip the t_ prefix) for registration & selfcheck.
def _public_name(fn) -> str:
    return fn.__name__[2:] if fn.__name__.startswith("t_") else fn.__name__


def build_server():
    """Create the FastMCP server with the appropriate tool set registered."""
    from mcp.server.fastmcp import FastMCP  # imported lazily; needs `mcp` SDK

    mcp = FastMCP("toss-trader")
    for fn in READ_TOOLS + PLAN_TOOLS:
        mcp.tool(name=_public_name(fn))(fn)

    # Catalog-name aliases (READ/PLAN only; same backing function, second name).
    for alias, fn in ALIASES.items():
        mcp.tool(name=alias)(fn)

    if CFG.allow_live_orders:
        for fn in WRITE_TOOLS:
            mcp.tool(name=_public_name(fn))(fn)
        # Write-tool catalog aliases (gated identically to WRITE_TOOLS).
        for alias, fn in WRITE_ALIASES.items():
            mcp.tool(name=alias)(fn)
    return mcp


def _selfcheck() -> int:
    """Offline smoke test — runs every tool in mock and prints a summary.

    Does NOT require the mcp SDK. Exits non-zero on any unhandled exception.
    """
    # HARD GUARD: selfcheck exercises the ORDER tools. With live+orders enabled
    # it would place REAL orders. Refuse — run with forced-mock env instead:
    #   env TOSS_LIVE=0 TOSS_CLIENT_ID= TOSS_CLIENT_SECRET= python3 server.py --selfcheck
    if CFG.live and CFG.allow_live_orders:
        print("[selfcheck] 거부: LIVE + 실주문 ON 상태에서는 selfcheck를 실행할 수 "
              "없습니다(테스트 주문이 실제 체결됨). forced-mock env로 실행하세요.")
        return 2
    print(f"[selfcheck] mode={CFG.mode} allow_live_orders={CFG.allow_live_orders}")
    print(f"[selfcheck] config={json.dumps(CFG.public_summary(), ensure_ascii=False)}")
    # Reset stateful guard files so the idempotency/limit assertions below are
    # deterministic across repeated selfcheck runs (test isolation only).
    try:
        for _p in (GUARD._dedup_path, GUARD._daily_path, GUARD._circuit_path):
            if _p.exists():
                _p.unlink()
    except Exception:  # noqa: BLE001
        pass
    # Test isolation: exercise the tool sweep under permissive limits so the
    # demo-sized sample orders aren't blocked by the operator's conservative
    # .env limits. The limit-gate LOGIC is verified in tests/test_order_safety.py.
    try:
        object.__setattr__(CFG, "max_order_krw", 100_000_000.0)
        object.__setattr__(CFG, "max_daily_krw", 1_000_000_000.0)
        object.__setattr__(CFG, "max_daily_count", 10_000)
    except Exception:  # noqa: BLE001
        pass
    failures = 0

    def show(label, value):
        s = json.dumps(value, ensure_ascii=False)
        print(f"  {label}: {s[:160]}{'…' if len(s) > 160 else ''}")

    try:
        show("account_info", t_get_account_info())
        show("resolve(엔비디아)", t_resolve_symbol("엔비디아"))
        show("quote(005930)", t_get_quote("005930"))
        show("prices", t_get_prices("005930,000660,NVDA"))
        show("orderbook", t_get_orderbook("005930"))
        show("candles", {"n": len(t_get_candles("005930", count=5).get("candles", []))})
        show("trades", {"n": len(t_get_trades("005930", 5).get("trades", []))})
        show("price_limits", t_get_price_limits("005930"))
        show("warnings", t_get_stock_warnings("005930"))
        # stock_info (new READ tool): master + market-cap + halt flags.
        si = t_stock_info("005930,NVDA")
        assert si.get("ok") and si["data"]["count"] == 2, "stock_info shape"
        show("stock_info", {"count": si["data"]["count"],
                            "first": (si["data"]["stocks"] or [{}])[0].get("symbol"),
                            "cap": (si["data"]["stocks"] or [{}])[0].get("marketCap")})
        show("fx", t_get_exchange_rate("USD", "KRW"))
        show("calendar_KR", {"sessions": len(t_get_market_calendar("KR").get("sessions", []))})
        show("holdings", t_get_holdings())
        show("buying_power", t_get_buying_power("KRW"))
        show("sellable", t_get_sellable_quantity("005930"))
        show("commissions", t_get_commission_policy())
        show("orders_OPEN", {"n": len(t_get_orders("OPEN").get("orders", []))})
        show("portfolio_overview", {"holdings": len(t_portfolio_overview().get("holdings", []))})
        show("analyze(엔비디아)", t_analyze_symbol("엔비디아").get("indicators"))

        # PLAN -> EXECUTE round-trip in mock. Derive an in-band, tick-aligned
        # LIMIT price from the live mock quote so validation passes.
        last_px = float(CLIENT.get_price("005930")["lastPrice"])
        tick = CLIENT.tick_size("005930", last_px)
        valid_px = int(last_px // tick * tick)  # round down to a tick boundary
        plan = t_plan_order("005930", "BUY", "LIMIT", quantity=2, price=valid_px)
        show("plan_order", {"token": bool(plan.get("preview_token")),
                            "phrase": plan.get("confirm_phrase")})
        token = plan.get("preview_token")
        phrase = plan.get("confirm_phrase")
        if token and phrase:
            # Wrong phrase must be rejected.
            bad = t_place_order_confirmed(token, "WRONG")
            assert "error" in bad, "expected confirm mismatch rejection"
            show("place(wrong phrase)", bad.get("error"))
            ok = t_place_order_confirmed(token, phrase, confirm_high_value=True)
            show("place(correct)", {"orderId": ok.get("orderId"),
                                    "status": ok.get("status"), "mode": ok.get("_mode")})
            # Token is single-use now.
            replay = t_place_order_confirmed(token, phrase)
            assert "error" in replay, "expected single-use rejection"
            show("place(replay blocked)", replay.get("error"))

        split = t_plan_split_order("엔비디아", "BUY", 1500.0, "USD", 3, "MARKET")
        show("plan_split", {"token": bool(split.get("preview_token")),
                            "slices": (split.get("batch") or {}).get("slices")})
        cancel = t_plan_cancel_all()
        show("plan_cancel_all", {"targets": len(cancel.get("targets", []))})

        # tickSize violation must be rejected by plan (in-band but misaligned).
        bad_tick = t_plan_order("005930", "BUY", "LIMIT", quantity=1,
                                price=valid_px + 1)
        show("plan(tick violation)", bad_tick.get("error", "NO ERROR (unexpected)"))

        # ---- new alpha READ tools (common {ok,data,_mock} envelope) ----
        def _assert_env(label, env):
            assert isinstance(env, dict) and "ok" in env, f"{label} not enveloped"
            show(label, {"ok": env.get("ok"),
                         "keys": list((env.get("data") or {}).keys())[:6]
                         if env.get("ok") else env.get("error")})
            return env

        _assert_env("orderbook_pressure", t_orderbook_pressure("005930"))
        _assert_env("intraday_vwap", t_intraday_vwap("005930"))
        _assert_env("recent_trades_tape", t_recent_trades_tape("005930"))
        _assert_env("price_limit_proximity", t_price_limit_proximity("005930"))
        _assert_env("scalp_score", t_scalp_score("005930,000660,NVDA"))
        _assert_env("supply_demand_flow", t_supply_demand_flow("005930"))
        _assert_env("supply_demand_flow(US)", t_supply_demand_flow("NVDA"))
        _assert_env("rank_watchlist", t_rank_watchlist(top_n=5))
        _assert_env("portfolio_risk_xray", t_portfolio_risk_xray())
        _assert_env("pnl_attribution", t_pnl_attribution())
        _assert_env("impact_of_order", t_impact_of_order("005930", "BUY", 10))
        # invariant 12: US symbol declared KRW -> CURRENCY_MISMATCH
        mism = t_impact_of_order("NVDA", "BUY", 1, currency="KRW")
        assert mism.get("ok") is False and \
            mism["error"]["code"] == "CURRENCY_MISMATCH", "expected mismatch"
        show("impact(currency mismatch)", mism.get("error"))

        # ---- new PLAN tools (calculation-only, no POST) ----
        _assert_env("plan_dca", t_plan_dca("005930", 500000, 6))
        _assert_env("plan_trailing_stop", t_plan_trailing_stop("005930", trail_pct=5))
        _assert_env("plan_rebalance",
                    t_plan_rebalance("005930:0.5,NVDA:0.5"))
        # rebalance weight-sum guard
        badrb = t_plan_rebalance("005930:0.5,NVDA:0.2")
        assert badrb.get("ok") is False, "expected weight-sum rejection"
        show("plan_rebalance(bad weights)", badrb.get("error"))

        # ---- safety gate 7 (idempotency) layered on EXECUTE ----
        p2 = t_plan_order("005930", "BUY", "LIMIT", quantity=3, price=valid_px)
        t2, ph2 = p2.get("preview_token"), p2.get("confirm_phrase")
        ok2 = t_place_order_confirmed(t2, ph2, confirm_high_value=True)
        show("place#2", {"orderId": ok2.get("orderId")})
        # Same intent again -> same clientOrderId -> DUPLICATE within window.
        p3 = t_plan_order("005930", "BUY", "LIMIT", quantity=3, price=valid_px)
        t3, ph3 = p3.get("preview_token"), p3.get("confirm_phrase")
        dup = t_place_order_confirmed(t3, ph3, confirm_high_value=True)
        assert dup.get("ok") is False and dup["error"]["code"] == "DUPLICATE", \
            f"expected DUPLICATE, got {dup}"
        show("place#3(duplicate blocked)", dup.get("error"))

        # ---- safety gate 4(ext): restricted-symbol block + warn surfacing ----
        # 000003 == 거래정지(koreanMarketDetail); 000002 == 투자위험(INVESTMENT_RISK);
        # 000001 == 정리매매(LIQUIDATION) — all must BLOCK at execute.
        for blk_sym, label in (("000003", "거래정지"), ("000002", "투자위험"),
                               ("000001", "정리매매")):
            last_b = float(CLIENT.get_price(blk_sym)["lastPrice"])
            tick_b = CLIENT.tick_size(blk_sym, last_b)
            px_b = int(last_b // tick_b * tick_b)
            pb = t_plan_order(blk_sym, "BUY", "LIMIT", quantity=1, price=px_b)
            tb, phb = pb.get("preview_token"), pb.get("confirm_phrase")
            # plan should already flag wouldBlock=True in the snapshot.
            assert pb.get("snapshot", {}).get("wouldBlock") is True, \
                f"plan should flag {label} restricted, got {pb.get('snapshot')}"
            rb = t_place_order_confirmed(tb, phb, confirm_high_value=True)
            assert rb.get("ok") is False and \
                rb["error"]["code"] == "RESTRICTED_SYMBOL", \
                f"expected RESTRICTED_SYMBOL for {label}, got {rb}"
            show(f"place({label} blocked)", rb.get("error", {}).get("code"))

        # OVERHEATED (000004) -> WARN, NOT block: order proceeds with _warnings.
        last_o = float(CLIENT.get_price("000004")["lastPrice"])
        tick_o = CLIENT.tick_size("000004", last_o)
        px_o = int(last_o // tick_o * tick_o)
        po = t_plan_order("000004", "BUY", "LIMIT", quantity=1, price=px_o)
        ro = t_place_order_confirmed(po.get("preview_token"),
                                     po.get("confirm_phrase"),
                                     confirm_high_value=True)
        assert ro.get("orderId") and ro.get("_warnings"), \
            f"OVERHEATED should warn-not-block, got {ro}"
        show("place(과열 warned)", {"orderId": bool(ro.get("orderId")),
                                     "warnings": ro.get("_warnings")})

        # ---- invariant 6: cancel of a terminal (FILLED) order is refused ----
        # MOCKORDexisting0002 is a canned FILLED order.
        state = _order_state_gate("MOCKORDexisting0002", "취소")
        assert state is not None and state.get("ok") is False and \
            state["error"]["code"] == "INVALID_PARAM", \
            f"expected terminal-order refusal, got {state}"
        show("cancel(terminal refused)", state.get("error", {}).get("message_ko"))
        # track_order surfaces the 한글 status interpretation.
        trk = t_track_order(order_id="MOCKORDexisting0001")
        show("track_order(statusKo)", {"status": trk.get("status"),
                                       "statusKo": trk.get("statusKo"),
                                       "cancelable": trk.get("isCancelable")})

        # alias resolution sanity (catalog names back the same functions)
        assert ALIASES["portfolio_snapshot"] is t_portfolio_overview
        assert ALIASES["plan_split_buy"] is t_plan_split_order
        show("aliases", {"names": list(ALIASES.keys()),
                         "analyze_symbol": _public_name(t_analyze_symbol)})
    except Exception as exc:  # noqa: BLE001
        failures += 1
        print(f"[selfcheck] FAILURE: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()

    write_registered = "REGISTERED" if CFG.allow_live_orders else "hidden (read-only)"
    print(f"[selfcheck] write tools: {write_registered}")
    print(f"[selfcheck] aliases: {len(ALIASES)} ({', '.join(ALIASES)})")
    print(f"[selfcheck] {'PASS' if failures == 0 else 'FAIL'} "
          f"({len(READ_TOOLS)} read, {len(PLAN_TOOLS)} plan, {len(WRITE_TOOLS)} write, "
          f"{len(ALIASES)} alias)")
    return 1 if failures else 0


def main() -> None:
    if "--selfcheck" in sys.argv:
        sys.exit(_selfcheck())
    server = build_server()
    server.run()  # stdio transport


if __name__ == "__main__":
    main()
