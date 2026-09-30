"""test_live_normalize.py — Live-shape -> canonical normalization regression.

Feeds *official-spec-shaped* live response payloads (candles with closePrice,
trades with no side and `volume` only, prices as an array, accounts as an array,
holdings with nested totals + items, orderbook with {price,volume} levels) into
:class:`toss_client._Normalize` and asserts that:

  1. The normalized shape matches the canonical (mock) contract keys.
  2. analytics / indicators consume the normalized output and produce sane
     numbers — i.e. the live field-name mismatch is actually fixed, not just
     re-labelled.
  3. trades `side` is *inferred* via the tick rule and flagged sideInferred.

Run: python3.12 tests/test_live_normalize.py   (no pytest dependency)
Pure stdlib; no network (these are synthetic spec-shaped payloads, not live).
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from toss_client import _Normalize, TossApiError, ERROR_CODE_MAP, RateGuard  # noqa: E402
import analytics  # noqa: E402
import indicators as ind  # noqa: E402
import safety  # noqa: E402

PASS = 0
FAIL = 0


def check(cond, msg):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS: {msg}")
    else:
        FAIL += 1
        print(f"  FAIL: {msg}")


# ---------------------------------------------------------------------------
# Official-spec-shaped live payloads (the "after key issuance" reality)
# ---------------------------------------------------------------------------

def live_candles(n=160, base=70000.0):
    """Live candles result: openPrice/highPrice/lowPrice/closePrice + nextBefore."""
    rows = []
    px = base
    for i in range(n):
        o = px
        c = px * (1 + (0.001 if i % 3 else -0.0008))
        rows.append({
            "timestamp": f"2026-01-{(i % 28) + 1:02d}T15:30:00+09:00",
            "openPrice": f"{round(o)}",
            "highPrice": f"{round(max(o, c) * 1.01)}",
            "lowPrice": f"{round(min(o, c) * 0.99)}",
            "closePrice": f"{round(c)}",
            "volume": str(1_000_000 + i * 1000),
            "currency": "KRW",
        })
        px = c
    return {"candles": rows, "nextBefore": "2026-01-01T15:30:00+09:00"}


LIVE_PRICES = [
    {"symbol": "005930", "timestamp": "2026-06-16T09:30:00+09:00",
     "lastPrice": "71500", "currency": "KRW"},
    {"symbol": "NVDA", "timestamp": "2026-06-16T09:30:00+09:00",
     "lastPrice": "880.25", "currency": "USD"},
]

# Live trades: a LIST, newest-first, NO side, volume (not quantity).
LIVE_TRADES = [
    {"price": "71500", "volume": "10", "timestamp": "2026-06-16T09:30:05+09:00",
     "currency": "KRW"},   # newest
    {"price": "71500", "volume": "5", "timestamp": "2026-06-16T09:30:04+09:00",
     "currency": "KRW"},   # equal -> keep prior side
    {"price": "71400", "volume": "8", "timestamp": "2026-06-16T09:30:03+09:00",
     "currency": "KRW"},   # downtick from 71450 -> SELL
    {"price": "71450", "volume": "3", "timestamp": "2026-06-16T09:30:02+09:00",
     "currency": "KRW"},   # uptick from 71300 -> BUY
    {"price": "71300", "volume": "20", "timestamp": "2026-06-16T09:30:01+09:00",
     "currency": "KRW"},   # oldest (default BUY)
]

LIVE_ORDERBOOK = {
    "timestamp": "2026-06-16T09:30:00+09:00", "currency": "KRW",
    "asks": [{"price": "71600", "volume": "100"},
             {"price": "71700", "volume": "80"}],
    "bids": [{"price": "71500", "volume": "150"},
             {"price": "71400", "volume": "120"}],
}

# Live accounts: a LIST (NOT {accounts:[...]}).
LIVE_ACCOUNTS = [
    {"accountNo": "12345678", "accountSeq": 7, "accountType": "BROKERAGE"},
]

LIVE_HOLDINGS = {
    "totalPurchaseAmount": {"krw": "5000000", "usd": "0"},
    "marketValue": {"amount": {"krw": "5600000", "usd": "0"},
                    "amountAfterCost": {"krw": "5590000", "usd": "0"}},
    "profitLoss": {"amount": {"krw": "600000"}, "rate": "12.0",
                   "amountAfterCost": {"krw": "590000"}, "rateAfterCost": "11.8"},
    "dailyProfitLoss": {"amount": {"krw": "30000"}, "rate": "0.5"},
    "items": [
        {"symbol": "005930", "name": "삼성전자", "quantity": 50,
         "purchasePrice": "70000", "currentPrice": "71500",
         "marketValue": "3575000", "profitLossAmount": "75000",
         "profitLossRatio": "2.14", "currency": "KRW"},
        {"symbol": "NVDA", "name": "엔비디아", "quantity": 5,
         "purchasePrice": "800.00", "currentPrice": "880.25",
         "marketValue": "4401.25", "profitLossAmount": "401.25",
         "profitLossRatio": "10.03", "currency": "USD"},
    ],
}

LIVE_PRICE_LIMITS = {"timestamp": "2026-06-16T09:00:00+09:00",
                     "upperLimitPrice": "92950", "lowerLimitPrice": "50050",
                     "currency": "KRW"}

LIVE_FX = {"baseCurrency": "USD", "quoteCurrency": "KRW", "rate": "1382.50",
           "midRate": "1382.00", "basisPoint": 50, "rateChangeType": "UP",
           "validFrom": "2026-06-16T09:00:00+09:00",
           "validUntil": "2026-06-16T09:01:00+09:00"}

LIVE_CALENDAR = {
    "today": {"date": "2026-06-16",
              "integrated": {
                  "preMarket": None,
                  "regularMarket": {"startTime": "09:00:00",
                                    "singlePriceAuctionStartTime": "08:30:00",
                                    "endTime": "15:30:00"},
                  "afterMarket": {"startTime": "16:00:00",
                                  "singlePriceAuctionStartTime": None,
                                  "endTime": "18:00:00"}}},
    "previousBusinessDay": "2026-06-15", "nextBusinessDay": "2026-06-17",
}

# Live GET /api/v1/stocks: an ARRAY of stock masters (openapi.json v1.1.1).
LIVE_STOCKS = [
    {"symbol": "005930", "name": "삼성전자", "englishName": "Samsung Electronics",
     "isinCode": "KR7005930003", "market": "KRX", "securityType": "STOCK",
     "isCommonShare": True, "status": "NORMAL", "currency": "KRW",
     "listDate": "1975-06-11", "delistDate": None,
     "sharesOutstanding": "5969782550", "leverageFactor": None,
     "koreanMarketDetail": {"liquidationTrading": False, "nxtSupported": True,
                            "krxTradingSuspended": False,
                            "nxtTradingSuspended": False}},
    {"symbol": "900001", "name": "정리매매종목", "englishName": "X",
     "isinCode": "KR7900001000", "market": "KRX", "securityType": "STOCK",
     "isCommonShare": True, "status": "DELISTING", "currency": "KRW",
     "listDate": "2010-01-04", "delistDate": "2026-07-31",
     "sharesOutstanding": "1000000", "leverageFactor": None,
     "koreanMarketDetail": {"liquidationTrading": True, "nxtSupported": False,
                            "krxTradingSuspended": False,
                            "nxtTradingSuspended": False}},
]

# Holdings with a per-row dailyProfitLoss (nested {amount:{krw}, rate}).
LIVE_HOLDINGS_DAILY = {
    "totalPurchaseAmount": {"krw": "5000000", "usd": "0"},
    "marketValue": {"amount": {"krw": "5600000", "usd": "0"}},
    "profitLoss": {"amount": {"krw": "600000"}, "rate": "12.0"},
    "dailyProfitLoss": {"amount": {"krw": "30000"}, "rate": "0.54"},
    "items": [
        {"symbol": "005930", "name": "삼성전자", "quantity": 50,
         "purchasePrice": "70000", "currentPrice": "71500",
         "marketValue": "3575000", "profitLossAmount": "75000",
         "profitLossRatio": "2.14", "currency": "KRW",
         "dailyProfitLoss": {"amount": {"krw": "12000"}, "rate": "0.34"}},
    ],
}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_candles():
    print("\n[candles] live closePrice -> canonical close")
    norm = _Normalize.candles(live_candles(), "005930", "1d")
    check("candles" in norm and norm["nextBefore"] is not None,
          "envelope has candles + nextBefore")
    c0 = norm["candles"][0]
    check(all(k in c0 for k in ("open", "high", "low", "close", "volume")),
          "row has short OHLC keys")
    check("closePrice" not in c0 and "openPrice" not in c0,
          "long *Price keys dropped")
    check(c0.get("currency") == "KRW", "currency preserved verbatim")
    # indicators must read the normalized candles and produce numbers.
    res = ind.compute(norm["candles"])
    check(res.get("last") is not None and res.get("rsi14") is not None,
          f"indicators.compute -> last={res.get('last')} rsi14={res.get('rsi14')}")
    check(res.get("ma20") is not None and res.get("bars") == 160,
          f"ma20 + bars correct (bars={res.get('bars')})")


def test_prices():
    print("\n[prices] live array -> {prices:[...]}")
    env = _Normalize.prices(LIVE_PRICES)
    check(isinstance(env, dict) and isinstance(env.get("prices"), list),
          "wrapped into {prices:[...]}")
    check(len(env["prices"]) == 2, "both rows kept")
    # analytics helper extracts a symbol's lastPrice from the envelope.
    lp = analytics._last_price_from_prices(env, "NVDA")
    check(lp == 880.25, f"_last_price_from_prices(NVDA) == 880.25 (got {lp})")
    # single price: live still returns an array -> first/matching row chosen.
    one = _Normalize.price(LIVE_PRICES, "005930")
    check(one.get("symbol") == "005930" and one.get("lastPrice") == "71500",
          f"single price picks matching row (lastPrice={one.get('lastPrice')})")


def test_trades():
    print("\n[trades] live array (no side) -> inferred side + sideInferred flag")
    env = _Normalize.trades(LIVE_TRADES, "005930")
    check(env.get("_sideInferred") is True, "envelope flagged _sideInferred")
    rows = env["trades"]
    check(len(rows) == 5 and env.get("symbol") == "005930", "5 rows, symbol set")
    check(all(r.get("sideInferred") is True for r in rows),
          "every row flagged sideInferred")
    check(all("quantity" in r for r in rows),
          "live volume mapped to canonical quantity")
    # rows are newest-first; map by the HH:MM:SS time component (drop tz).
    def _hms(ts):
        return ts.split("T", 1)[1][:8]
    by_ts = {_hms(r["timestamp"]): r for r in rows}
    check(by_ts["09:30:01"]["side"] == "BUY", "oldest defaults BUY")
    check(by_ts["09:30:02"]["side"] == "BUY", "uptick 71300->71450 = BUY")
    check(by_ts["09:30:03"]["side"] == "SELL", "downtick 71450->71400 = SELL")
    check(by_ts["09:30:04"]["side"] == "BUY", "uptick 71400->71500 = BUY")
    check(by_ts["09:30:05"]["side"] == "BUY", "equal 71500==71500 keeps BUY")
    # indicators.trade_strength must work off the inferred sides.
    ts = ind.trade_strength(rows)
    check(ts is not None, f"trade_strength computable on inferred sides ({ts})")
    # analytics.recent_trades_tape consumes the canonical envelope.
    tape = analytics.recent_trades_tape(env)
    check(tape.get("count") == 5 and tape.get("buyVolume") > 0,
          f"recent_trades_tape: count={tape.get('count')} "
          f"buyVol={tape.get('buyVolume')} sellVol={tape.get('sellVolume')}")


def test_orderbook():
    print("\n[orderbook] live {price,volume} -> {price,quantity}")
    ob = _Normalize.orderbook(LIVE_ORDERBOOK, "005930")
    check(ob["bids"][0].get("quantity") == "150",
          "bid volume mapped to quantity")
    check(ob["asks"][0].get("quantity") == "100", "ask volume mapped")
    check(ob.get("symbol") == "005930" and ob.get("currency") == "KRW",
          "symbol injected, currency preserved")
    press = analytics.orderbook_pressure(ob)
    check(press.get("bestBid") == 71500.0 and press.get("bestAsk") == 71600.0,
          f"orderbook_pressure best bid/ask ({press.get('bestBid')}/"
          f"{press.get('bestAsk')})")
    check(press.get("imbalance") is not None,
          f"imbalance computed = {press.get('imbalance')} ({press.get('pressure')})")


def test_accounts():
    print("\n[accounts] live LIST -> {accounts:[...]} (no .get crash)")
    env = _Normalize.accounts(LIVE_ACCOUNTS)
    check(isinstance(env, dict) and len(env.get("accounts", [])) == 1,
          "wrapped to dict with 1 account")
    check(env["accounts"][0]["accountSeq"] == 7, "accountSeq preserved")
    # Simulate the exact account_seq() indexing path that previously crashed.
    accounts = env.get("accounts", [])
    seq = int(accounts[0]["accountSeq"])
    check(seq == 7, f"account_seq indexing works (seq={seq})")


def test_holdings():
    print("\n[holdings] nested totals + items -> {holdings:[...], summary}")
    env = _Normalize.holdings(LIVE_HOLDINGS)
    rows = env["holdings"]
    check(len(rows) == 2, "2 holding rows")
    check(all("evaluationAmount" in r for r in rows),
          "marketValue -> evaluationAmount bridged")
    check(all("profitLoss" in r and "profitLossRate" in r for r in rows),
          "profitLossAmount/Ratio bridged")
    # portfolio analytics must consume the normalized holdings.
    xray = analytics.portfolio_risk_xray(env, usdkrw=1382.5)
    check(xray.get("totalEquityKRW", 0) > 0,
          f"risk_xray totalEquityKRW = {xray.get('totalEquityKRW')}")
    check(len(xray.get("positions", [])) == 2,
          "risk_xray sees both positions")
    pnl = analytics.pnl_attribution(env, usdkrw=1382.5)
    check(pnl.get("totalPnLKRW", 0) != 0,
          f"pnl_attribution totalPnLKRW = {pnl.get('totalPnLKRW')}")
    check(env["summary"].get("totalEvaluationKRW") is not None,
          f"summary built from live totals "
          f"(eval={env['summary'].get('totalEvaluationKRW')})")


def test_price_limits():
    print("\n[price-limits] field passthrough + symbol inject")
    pl = _Normalize.price_limits(LIVE_PRICE_LIMITS, "005930")
    check(pl.get("upperLimitPrice") == "92950" and
          pl.get("lowerLimitPrice") == "50050", "limits preserved")
    prox = analytics.price_limit_proximity(pl, 71500.0, symbol="005930",
                                           currency="KRW")
    check(prox.get("upperLimit") == 92950.0 and prox.get("upRoomPct") is not None,
          f"proximity computed (upRoom={prox.get('upRoomPct')}%)")


def test_fx_and_calendar():
    print("\n[exchange-rate + market-calendar] bridging")
    fx = _Normalize.exchange_rate(LIVE_FX, "USD", "KRW")
    check(fx.get("rate") == "1382.50" and fx.get("validUntil") is not None,
          "fx rate + validUntil preserved")
    cal = _Normalize.market_calendar(LIVE_CALENDAR, "KR")
    check(cal.get("date") == "2026-06-16" and cal.get("isOpen") is True,
          f"calendar flattened (isOpen={cal.get('isOpen')})")
    names = {s["name"] for s in cal.get("sessions", [])}
    check("REGULAR" in names and "AFTER" in names and "PRE" not in names,
          f"only non-null sessions surfaced ({sorted(names)})")
    reg = next(s for s in cal["sessions"] if s["name"] == "REGULAR")
    check(reg["open"] == "09:00:00" and reg["close"] == "15:30:00",
          "REGULAR start/end mapped")


def test_intraday_vwap():
    print("\n[intraday_vwap] normalized 1m candles feed VWAP")
    norm = _Normalize.candles(live_candles(n=60, base=71000.0), "005930", "1m")
    vwap = analytics.intraday_vwap(norm, prev_close=70800.0)
    check("error" not in vwap and vwap.get("vwapNow") is not None,
          f"vwapNow computed = {vwap.get('vwapNow')}")
    check(vwap.get("bars", 0) > 0, f"bars={vwap.get('bars')}")


def test_stocks():
    print("\n[stocks] live ARRAY -> {stocks:[...]} + restriction classification")
    env = _Normalize.stocks(LIVE_STOCKS)
    check(isinstance(env, dict) and isinstance(env.get("stocks"), list),
          "wrapped into {stocks:[...]}")
    check(len(env["stocks"]) == 2, "both masters kept")
    s0 = env["stocks"][0]
    check(s0.get("sharesOutstanding") == "5969782550",
          "sharesOutstanding kept as string")
    check(s0.get("securityType") == "STOCK" and s0.get("isCommonShare") is True,
          "securityType / isCommonShare preserved")
    # sharesOutstanding floatable for market-cap math.
    check(float(s0["sharesOutstanding"]) > 0, "sharesOutstanding float()-able")
    # restriction classification: normal symbol not blocked, 정리매매 blocked.
    cls0 = safety.classify_symbol_restrictions(
        [], LIVE_STOCKS[0]["koreanMarketDetail"])
    check(cls0["blocked"] is False, "normal symbol not blocked")
    cls1 = safety.classify_symbol_restrictions(
        [], LIVE_STOCKS[1]["koreanMarketDetail"])
    check(cls1["blocked"] is True and "정리매매(거래소)" in cls1["block"],
          f"정리매매 symbol blocked ({cls1['block']})")
    # INVESTMENT_RISK warning blocks; OVERHEATED only warns.
    rsk = safety.classify_symbol_restrictions(
        [{"warningType": "INVESTMENT_RISK", "label": "투자위험"}], None)
    check(rsk["blocked"] is True, "INVESTMENT_RISK blocks")
    oh = safety.classify_symbol_restrictions(
        [{"warningType": "OVERHEATED", "label": "단기과열"}], None)
    check(oh["blocked"] is False and "단기과열" in oh["warn"],
          "OVERHEATED warns but does not block")


def test_holdings_daily_pnl():
    print("\n[holdings] dailyProfitLoss per-row + summary rate mapping")
    env = _Normalize.holdings(LIVE_HOLDINGS_DAILY)
    row = env["holdings"][0]
    check(row.get("dailyProfitLoss") == "12000",
          f"row dailyProfitLoss flattened ({row.get('dailyProfitLoss')})")
    check(row.get("dailyProfitLossRate") == "0.34",
          f"row dailyProfitLossRate mapped ({row.get('dailyProfitLossRate')})")
    summ = env["summary"]
    check(summ.get("dailyProfitLossKRW") == "30000",
          f"summary dailyProfitLossKRW ({summ.get('dailyProfitLossKRW')})")
    check(summ.get("dailyProfitLossRate") == "0.54",
          f"summary dailyProfitLossRate ({summ.get('dailyProfitLossRate')})")


def test_error_mapping():
    print("\n[error] live error code mapping + requestId preservation")
    # _parse_envelope on a live {error:{requestId,code,message}}.
    try:
        _Normalize  # noqa
        from toss_client import TossClient  # noqa: E402
        TossClient._parse_envelope(
            {"error": {"requestId": "req-123", "code": "stock-not-found",
                       "message": "종목 없음"}})
        check(False, "expected TossApiError raised")
    except TossApiError as e:
        check(e.code == "INVALID_PARAM", f"stock-not-found -> INVALID_PARAM ({e.code})")
        check(e.request_id == "req-123", f"requestId preserved ({e.request_id})")
        check(e.to_dict()["error"].get("requestId") == "req-123",
              "to_dict() surfaces requestId")
    # mapping table sanity.
    check(ERROR_CODE_MAP["exchange-rate-not-found"] == "INVALID_PARAM",
          "exchange-rate-not-found mapped")
    check(ERROR_CODE_MAP["invalid_client"] == "auth-error",
          "invalid_client -> auth-error")
    check(ERROR_CODE_MAP["account-header-required"] == "config-error",
          "account-header-required -> config-error")


def test_rate_header_case():
    print("\n[rate-guard] lowercase x-ratelimit-* honoured")
    rg = RateGuard()
    # Lowercase headers (Toss reality) must throttle the group.
    rg.observe("STOCK", {"x-ratelimit-remaining": "0",
                         "x-ratelimit-reset": str(__import__("time").time() + 5)})
    import time as _t
    wait = rg._next_ok.get("STOCK", 0) - _t.time()
    check(wait > 0, f"lowercase x-ratelimit-* set a backoff (wait={round(wait,1)}s)")
    # Retry-After (any case) honoured too.
    rg2 = RateGuard()
    rg2.observe("ORDER", {"Retry-After": "3"})
    check(rg2._next_ok.get("ORDER", 0) - _t.time() > 0,
          "Retry-After honoured")
    # global token bucket (inv 9c): even DIFFERENT groups are paced.
    rg3 = RateGuard()
    t0 = _t.time()
    rg3.before("G1")
    rg3.before("G2")
    dt = _t.time() - t0
    check(dt >= RateGuard.GLOBAL_MIN_INTERVAL * 0.8,
          f"global bucket spaces cross-group calls ({dt:.3f}s)")


def test_warnings_and_commissions():
    print("\n[warnings/commissions] live ARRAY -> dict envelope (was unnormalized)")
    # warnings: live returns a bare LIST; mock returns {symbol, warnings:[...]}.
    # The gap crashed analyze_symbol, portfolio_overview AND the order gate's
    # .get("warnings") -> fail-open on restricted symbols. Lock the wrap in.
    env = _Normalize.warnings([], "005930")
    check(isinstance(env, dict) and env.get("warnings") == [],
          "empty live warnings list -> {symbol, warnings:[]}")
    check(env.get("symbol") == "005930", "symbol carried")
    live_rows = [{"warningType": "INVESTMENT_RISK", "label": "투자위험"},
                 {"warningType": "OVERHEATED", "label": "단기과열"}]
    env2 = _Normalize.warnings(live_rows, "000002")
    check(len(env2["warnings"]) == 2, "live warning rows preserved")
    # The wrapped list feeds the classifier (reads live warningType) and BLOCKS.
    cls = safety.classify_symbol_restrictions(env2["warnings"], None)
    check(cls["blocked"] is True, "wrapped live warnings still BLOCK 투자위험")
    # idempotent: a dict already in canonical shape is left intact.
    again = _Normalize.warnings({"symbol": "X", "warnings": live_rows}, "X")
    check(len(again["warnings"]) == 2, "dict-shaped input tolerated")

    # commissions: live ARRAY of {marketCountry, commissionRate,...} -> {policies}.
    live_comm = [{"marketCountry": "KR", "commissionRate": "0",
                  "startDate": "2025-12-15", "endDate": "2026-06-30"},
                 {"marketCountry": "US", "commissionRate": "0.1",
                  "startDate": None, "endDate": "2026-06-30"}]
    cenv = _Normalize.commissions(live_comm)
    check(isinstance(cenv.get("policies"), list) and len(cenv["policies"]) == 2,
          "live commissions ARRAY -> {policies:[...]}")
    check(cenv["policies"][0].get("market") == "KR",
          "marketCountry aliased to market")
    check(cenv["policies"][0].get("commissionRate") == "0",
          "live commissionRate field kept verbatim")

    # buying-power: live {cashBuyingPower} -> alias availableAmount (else live
    # buys read cash=0 and every order is wrongly rejected).
    bp = _Normalize.buying_power({"currency": "KRW", "cashBuyingPower": "12500000"}, "KRW")
    check(bp.get("availableAmount") == "12500000",
          "cashBuyingPower aliased to availableAmount")
    check(float(bp["availableAmount"]) == 12500000.0, "availableAmount float()-able")
    # sellable-quantity: live STRING "5" -> number (SELL compares quantity>sq).
    sq = _Normalize.sellable_quantity({"sellableQuantity": "5"}, "005930")
    check(sq["sellableQuantity"] == 5 and not isinstance(sq["sellableQuantity"], str),
          "live sellableQuantity string -> number")
    check(_Normalize.sellable_quantity({}, "X")["sellableQuantity"] == 0,
          "missing sellableQuantity -> 0")


def test_candles_sorted():
    print("\n[candles] NEWEST-FIRST live feed -> sorted OLDEST-FIRST (indicator-order fix)")
    # The live feed returns candles newest-first; indicators need oldest-first.
    desc = {"candles": [
        {"timestamp": "2026-03-03T15:30:00+09:00", "openPrice": "300",
         "highPrice": "301", "lowPrice": "299", "closePrice": "300", "volume": "3"},
        {"timestamp": "2026-03-02T15:30:00+09:00", "openPrice": "200",
         "highPrice": "201", "lowPrice": "199", "closePrice": "200", "volume": "2"},
        {"timestamp": "2026-03-01T15:30:00+09:00", "openPrice": "100",
         "highPrice": "101", "lowPrice": "99", "closePrice": "100", "volume": "1"},
    ], "nextBefore": "cursor"}
    norm = _Normalize.candles(desc, "X", "1d")
    ts = [c["timestamp"] for c in norm["candles"]]
    check(ts == sorted(ts), "candles sorted ascending by timestamp")
    check(norm["candles"][0]["close"] == "100" and norm["candles"][-1]["close"] == "300",
          "oldest bar first, newest bar last (compute's 'last' = newest close)")
    # indicators on the (now correctly ordered) series: last must be the NEWEST.
    res = ind.compute(norm["candles"])
    check(res.get("last") == 300, f"indicators.last = newest close ({res.get('last')})")


def test_order_body_and_drift():
    print("\n[order-body/drift] whitelist+stringify + price-drift gate (money path)")
    from toss_client import TossClient
    # Official OrderCreateRequest: only whitelisted keys, numeric fields as
    # STRINGS, no internal/junk fields, no market/currency in the body.
    snap = {
        "symbol": "005930", "side": "BUY", "orderType": "LIMIT", "quantity": 3,
        "orderAmount": None, "price": 70000, "timeInForce": "DAY",
        "currency": "KRW", "estimatedAmount": 210000, "snapshotPrice": 70000.0,
        "highValue": False, "account_index": 0, "warnings": [],
        "restrictedBlock": [], "wouldBlock": False, "notes": None,
        "clientOrderId": "COID123", "confirmHighValueOrder": True,
    }
    body = TossClient._order_body(snap, TossClient._ORDER_BODY_KEYS)
    check(set(body) <= set(TossClient._ORDER_BODY_KEYS), "only whitelisted keys posted")
    for junk in ("warnings", "estimatedAmount", "snapshotPrice", "account_index",
                 "restrictedBlock", "currency", "highValue"):
        check(junk not in body, f"internal field '{junk}' NOT posted")
    check(body["price"] == "70000" and body["quantity"] == "3",
          "KRW numbers -> strings without trailing .0")
    check(body["confirmHighValueOrder"] is True, "confirmHighValueOrder kept")
    # US fractional: 0.5 / 100.5 stay precise as strings; None price dropped.
    b2 = TossClient._order_body(
        {"symbol": "NVDA", "side": "BUY", "orderType": "MARKET",
         "orderAmount": 100.5, "quantity": 0.5, "price": None,
         "timeInForce": "DAY", "clientOrderId": "C2"},
        TossClient._ORDER_BODY_KEYS)
    check(b2["orderAmount"] == "100.5" and b2["quantity"] == "0.5",
          "US fractional -> precise strings")
    check("price" not in b2, "None fields dropped from body")
    # modify body excludes symbol/side/orderType.
    bm = TossClient._order_body(snap, TossClient._MODIFY_BODY_KEYS)
    check("symbol" not in bm and "side" not in bm, "modify body omits symbol/side")

    # invariant 13B — price-drift thresholds.
    check(safety.check_price_drift(70000, 70300) is None, "within 1% -> proceed")
    d = safety.check_price_drift(70000, 71000)  # +1.43%
    check(d is not None and d["error"]["code"] == "PRICE_MOVED", ">1% -> PRICE_MOVED")
    check(safety.check_price_drift(None, 70000) is None
          and safety.check_price_drift(70000, None) is None,
          "missing price -> no block (caller decides)")


if __name__ == "__main__":
    test_candles()
    test_prices()
    test_trades()
    test_orderbook()
    test_accounts()
    test_holdings()
    test_price_limits()
    test_fx_and_calendar()
    test_intraday_vwap()
    test_stocks()
    test_holdings_daily_pnl()
    test_warnings_and_commissions()
    test_candles_sorted()
    test_order_body_and_drift()
    test_error_mapping()
    test_rate_header_case()
    print(f"\n=== live-normalize sim: {PASS} passed, {FAIL} failed ===")
    sys.exit(1 if FAIL else 0)
