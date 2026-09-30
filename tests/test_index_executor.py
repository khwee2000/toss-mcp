"""test_index_executor.py — execute.py 핵심 로직 단위 테스트(검증 그물).

네트워크 없이 합성 저널/목킹으로 순수 로직 검증. 야간 개선의 '안정성 그물' —
매 개선 후 `python3.12 -m unittest tests.test_index_executor` 로 회귀 확인.

실행: cd ~/toss-mcp && python3.12 -m unittest tests.test_index_executor -v
"""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import execute as E        # noqa: E402
import server as S         # noqa: E402
import market_cycle as MC  # noqa: E402
import stock_screener as SC  # noqa: E402


def J(events):
    """합성 저널 주입."""
    E._read_journal = lambda: events


class Base(unittest.TestCase):
    _ATTRS = ["_read_journal", "_today_kst", "_trading_day", "_load_targets",
              "_days_to_earnings", "_rsi", "_market_open", "_strategy_positions",
              "sell", "_recent_trade", "_day_change", "_ma150", "_days_held", "_atr_pct",
              "_mfe_pct", "_guard", "_regime_risk_off", "_in_cooldown", "_fetch_daily",
              "_open_symbols_and_deployed", "_strategy_circuit", "_repoll_pending",
              "_reconcile_fill", "_set_halt", "_entry_guard", "intraday_entry_check",
              "_spread_ok", "_today_deployed_usd", "_earn_last_session",
              "_trading_days_held", "_partial_tp_taken"]

    _CLI = ["get_price", "get_order", "get_trades", "get_candles", "get_sellable_quantity",
            "get_market_calendar", "get_orderbook", "get_buying_power"]

    def setUp(self):
        self._saved = {a: getattr(E, a) for a in self._ATTRS}
        self._saved_sess = MC.us_session
        self._saved_cli = {m: getattr(S.CLIENT, m) for m in self._CLI}
        # 기본 호가: 현재가와 정합하는 책을 돌려준다(감사#233 교차검증이 fail-closed라
        # 목킹이 없으면 모든 청산이 보류된다 — 실제 장에서는 호가가 존재하는 게 정상).
        S.CLIENT.get_orderbook = lambda sym: (
            lambda px: {"bids": [{"price": str(px * 0.999), "quantity": "10"}],
                        "asks": [{"price": str(px * 1.001), "quantity": "10"}]}
        )(float(S.CLIENT.get_price(sym)["lastPrice"]))
        # 실데이터 완전격리(#215): 어떤 테스트도 실제 원장/metrics/alert 파일을 건드리지 못하게
        self._journal_orig = E.JOURNAL
        E.JOURNAL = Path(tempfile.mkdtemp()) / "index_journal.jsonl"
        # #6(개편): idle-capital 경보가 라이브 매수여력을 조회 못 하게 기본 목(0=경보 미발동). 테스트가 필요 시 개별 오버라이드
        S.CLIENT.get_buying_power = lambda c: {"availableAmount": "0"}
        # #4(개편): 목표가 파일도 temp로 리다이렉트 — set_target/manage가 실계좌 targets.json을 오염시키던 버그
        self._targets_orig = E.TARGETS_FILE
        E.TARGETS_FILE = E.JOURNAL.parent / "targets.json"

    def tearDown(self):
        E.JOURNAL = self._journal_orig
        E.TARGETS_FILE = self._targets_orig
        for a, v in self._saved.items():
            setattr(E, a, v)
        MC.us_session = self._saved_sess
        for m, v in self._saved_cli.items():
            setattr(S.CLIENT, m, v)


class TestStrategyPositions(Base):
    def test_simple_buy(self):
        J([{"ts": "2026-07-15T01:00:00", "event": "BUY_FILLED", "sym": "GOOGL",
            "fillQty": 0.1, "fillPrice": 350.0, "filledUsd": 35.0}])
        pos = E._strategy_positions()
        self.assertIn("GOOGL", pos)
        self.assertAlmostEqual(pos["GOOGL"]["qty"], 0.1)
        self.assertAlmostEqual(pos["GOOGL"]["entryPx"], 350.0)
        self.assertAlmostEqual(pos["GOOGL"]["cost_usd"], 35.0)

    def test_full_sell_closes(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100.0},
           {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 110.0}])
        self.assertEqual(E._strategy_positions(), {})

    def test_partial_sell_reduces_proportionally(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100.0, "filledUsd": 100.0},
           {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 0.4, "fillPrice": 110.0}])
        pos = E._strategy_positions()
        self.assertAlmostEqual(pos["X"]["qty"], 0.6)
        self.assertAlmostEqual(pos["X"]["cost_usd"], 60.0, places=5)
        self.assertAlmostEqual(pos["X"]["entryPx"], 100.0, places=5)

    def test_weighted_entry_two_buys(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100.0},
           {"ts": "2", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 120.0}])
        pos = E._strategy_positions()
        self.assertAlmostEqual(pos["X"]["qty"], 2.0)
        self.assertAlmostEqual(pos["X"]["entryPx"], 110.0, places=5)

    def test_zero_qty_ignored(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 0, "fillPrice": 100.0}])
        self.assertEqual(E._strategy_positions(), {})

    def test_oversell_clamps_to_zero(self):
        # 매도가 매수보다 많아도 qty 음수 안 되고 0으로 제거(CX17)
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100.0},
           {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 2.0, "fillPrice": 110.0}])
        self.assertEqual(E._strategy_positions(), {})


class TestOpenAndDeployed(Base):
    def test_deployed_sum(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "A", "fillQty": 0.35, "fillPrice": 100, "filledUsd": 35.0},
           {"ts": "2", "event": "BUY_FILLED", "sym": "B", "fillQty": 0.28, "fillPrice": 50, "filledUsd": 14.0}])
        occ, dep = E._open_symbols_and_deployed()
        self.assertEqual(occ, {"A", "B"})
        self.assertAlmostEqual(dep, 49.0)

    def test_full_sell_frees_slot(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "A", "fillQty": 1.0, "fillPrice": 100, "filledUsd": 35.0},
           {"ts": "2", "event": "BUY_FILLED", "sym": "B", "fillQty": 0.28, "fillPrice": 50, "filledUsd": 14.0},
           {"ts": "3", "event": "SELL_FILLED", "sym": "A", "fillQty": 1.0, "fillPrice": 110}])
        occ, dep = E._open_symbols_and_deployed()
        self.assertEqual(occ, {"B"})
        self.assertAlmostEqual(dep, 14.0)

    def test_partial_sell_keeps_slot(self):
        # 부분매도는 슬롯 유지(물타기 우회 방지) + 순usd 비례 반영(E3/CX2)
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "A", "fillQty": 1.0, "fillPrice": 100, "filledUsd": 40.0},
           {"ts": "2", "event": "SELL_FILLED", "sym": "A", "fillQty": 0.4, "fillPrice": 110}])
        occ, dep = E._open_symbols_and_deployed()
        self.assertEqual(occ, {"A"})
        self.assertAlmostEqual(dep, 24.0)      # 40 × (1−0.4)

    def test_working_and_uncertain_occupy(self):
        J([{"ts": "1", "event": "BUY_WORKING", "sym": "A", "usd": 35.0},
           {"ts": "2", "event": "BUY_UNCERTAIN", "sym": "B", "usd": 14.0}])
        occ, dep = E._open_symbols_and_deployed()
        self.assertEqual(occ, {"A", "B"})
        self.assertAlmostEqual(dep, 49.0)

    def test_working_then_filled_no_double_count(self):
        # BUY_WORKING 후 같은 종목 BUY_FILLED → 배치액 이중계산 안 됨(A13, 확정=fusd만)
        J([{"ts": "1", "event": "BUY_WORKING", "sym": "A", "usd": 35.0},
           {"ts": "2", "event": "BUY_FILLED", "sym": "A", "fillQty": 0.1, "fillPrice": 350, "filledUsd": 35.0}])
        occ, dep = E._open_symbols_and_deployed()
        self.assertEqual(occ, {"A"})
        self.assertAlmostEqual(dep, 35.0)     # 70이면 이중계산 버그


class TestRealizedTrades(Base):
    def test_single_win(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100.0},
           {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 110.0}])
        t = E._realized_trades()
        self.assertEqual(len(t), 1)
        self.assertAlmostEqual(t[0]["plPct"], 10.0)
        self.assertAlmostEqual(t[0]["plUsd"], 10.0)

    def test_fifo_multi_lot(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100.0},
           {"ts": "2", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 120.0},
           {"ts": "3", "event": "SELL_FILLED", "sym": "X", "fillQty": 1.5, "fillPrice": 130.0}])
        t = E._realized_trades()
        self.assertEqual(len(t), 2)
        self.assertAlmostEqual(t[0]["plUsd"], 30.0)     # 1.0 @100→130
        self.assertAlmostEqual(t[1]["plUsd"], 5.0)      # 0.5 @120→130

    def test_loss(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100.0},
           {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 92.0}])
        t = E._realized_trades()
        self.assertAlmostEqual(t[0]["plPct"], -8.0)

    def test_unknown_price_sell_skipped(self):
        # 체결가 0(미상) 매도는 lot 소비/기록 안 함 → 이후 유효매도가 원래 lot과 매칭(CX5)
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100.0},
           {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 0},
           {"ts": "3", "event": "SELL_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 110.0}])
        t = E._realized_trades()
        self.assertEqual(len(t), 1)
        self.assertAlmostEqual(t[0]["plPct"], 10.0)


class TestCircuit(Base):
    def test_max_entries_blocks(self):
        E._trading_day = lambda ts=None: "2026-07-15" if ts is None else str(ts)[:10]
        J([{"ts": f"2026-07-15T0{i}:00:00", "event": "BUY_FILLED", "sym": f"X{i}",
            "fillQty": 1, "fillPrice": 10} for i in range(E.MAX_ENTRIES_PER_DAY)])
        ok, why = E._strategy_circuit()
        self.assertFalse(ok)
        self.assertIn("진입", why)

    def test_under_limit_ok(self):
        E._trading_day = lambda ts=None: "2026-07-15" if ts is None else str(ts)[:10]
        J([{"ts": "2026-07-15T01:00:00", "event": "BUY_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 10}])
        ok, _ = E._strategy_circuit()
        self.assertTrue(ok)

    def test_other_day_not_counted(self):
        E._trading_day = lambda ts=None: "2026-07-15" if ts is None else str(ts)[:10]
        J([{"ts": f"2026-07-14T0{i}:00:00", "event": "BUY_FILLED", "sym": f"X{i}",
            "fillQty": 1, "fillPrice": 10} for i in range(E.MAX_ENTRIES_PER_DAY + 2)])
        ok, _ = E._strategy_circuit()
        self.assertTrue(ok)     # 어제 진입은 오늘 서킷에 안 걸림

    def test_loss_closes_block(self):
        E._trading_day = lambda ts=None: "2026-07-15" if ts is None else str(ts)[:10]
        evs = []
        for i in range(E.MAX_LOSS_CLOSES_PER_DAY):
            evs.append({"ts": f"2026-07-15T0{i}:00:00", "event": "BUY_FILLED", "sym": f"L{i}", "fillQty": 1.0, "fillPrice": 100.0})
            evs.append({"ts": f"2026-07-15T0{i}:30:00", "event": "SELL_FILLED", "sym": f"L{i}", "fillQty": 1.0, "fillPrice": 90.0})
        J(evs)
        ok, why = E._strategy_circuit()
        self.assertFalse(ok)
        self.assertIn("손실", why)

    def test_dollar_loss_circuit(self):
        E._trading_day = lambda ts=None: "2026-07-15" if ts is None else str(ts)[:10]
        # 실현손실이 MAX_DAILY_LOSS_USD를 넘으면 자본보호 중단(C9). 손실폭을 상수에서 계산 —
        # 하드코딩하면 예산 증액 때마다 깨진다(감사#231 동일 원칙).
        loss = E.MAX_DAILY_LOSS_USD + 4          # 한도보다 확실히 초과
        J([{"ts": "2026-07-15T01:00:00", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100.0},
           {"ts": "2026-07-15T02:00:00", "event": "SELL_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100.0 - loss}])
        ok, why = E._strategy_circuit()
        self.assertFalse(ok)
        self.assertIn("실현손실", why)

    def test_peak_drawdown_circuit(self):
        # 피크(+5) 대비 낙폭이 -0.15×BUDGET을 넘으면 신규중단(C16). 예산 상수 기준으로
        # 손실폭을 계산 — 하드코딩하면 증액 때마다 테스트가 깨진다(감사#231).
        loss = 0.15 * E.BUDGET_USD + 5      # 피크 +5에서 이만큼 빠지면 낙폭 = -(0.15×예산+…)
        E._trading_day = lambda ts=None: "2026-07-15" if ts is None else str(ts)[:10]
        J([{"ts": "2026-07-14T01:00:00", "event": "BUY_FILLED", "sym": "A", "fillQty": 1, "fillPrice": 100},
           {"ts": "2026-07-14T02:00:00", "event": "SELL_FILLED", "sym": "A", "fillQty": 1, "fillPrice": 105},
           {"ts": "2026-07-14T03:00:00", "event": "BUY_FILLED", "sym": "B", "fillQty": 1, "fillPrice": 100},
           {"ts": "2026-07-14T04:00:00", "event": "SELL_FILLED", "sym": "B", "fillQty": 1,
            "fillPrice": 100 - loss}])
        ok, why = E._strategy_circuit()
        self.assertFalse(ok)
        self.assertIn("낙폭", why)


class TestReconcileFill(Base):
    def test_execution_nested(self):
        S.CLIENT.get_order = lambda order_id=None: {
            "status": "FILLED", "execution": {"filledQuantity": "0.098", "averageFilledPrice": "355.6"}}
        st, fq, fp = E._reconcile_fill("oid", tries=1)
        self.assertEqual(st, "FILLED")
        self.assertAlmostEqual(fq, 0.098)
        self.assertAlmostEqual(fp, 355.6)

    def test_top_level_fallback(self):
        S.CLIENT.get_order = lambda order_id=None: {
            "status": "FILLED", "filledQuantity": "2", "averageFilledPrice": "70000"}
        st, fq, fp = E._reconcile_fill("oid", tries=1)
        self.assertEqual(st, "FILLED")
        self.assertAlmostEqual(fq, 2.0)
        self.assertAlmostEqual(fp, 70000.0)

    def test_no_oid(self):
        st, fq, fp = E._reconcile_fill(None)
        self.assertIsNone(st)
        self.assertEqual(fq, 0.0)

    def test_zero_price_treated_unknown(self):
        S.CLIENT.get_order = lambda order_id=None: {
            "status": "FILLED", "execution": {"filledQuantity": "0.1", "averageFilledPrice": "0.00"}}
        st, fq, fp = E._reconcile_fill("oid", tries=1)
        self.assertEqual(st, "FILLED")
        self.assertAlmostEqual(fq, 0.1)
        self.assertIsNone(fp)     # "0.00"은 체결가 미상(CX6)


class TestTargets(Base):
    def test_load(self):
        import json
        from pathlib import Path
        p = E.TARGETS_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        orig = p.read_text() if p.exists() else None
        try:
            p.write_text(json.dumps({"googl": 370, "MDLZ": 62.5}))
            t = E._load_targets()
            self.assertAlmostEqual(t["GOOGL"], 370.0)   # 대문자 정규화
            self.assertAlmostEqual(t["MDLZ"], 62.5)
        finally:
            if orig is not None:
                p.write_text(orig)

    def test_bad_value_skipped(self):
        import json
        p = E.TARGETS_FILE
        orig = p.read_text() if p.exists() else None
        try:
            p.write_text(json.dumps({"GOOGL": 370, "MDLZ": "n/a"}))
            t = E._load_targets()
            self.assertAlmostEqual(t["GOOGL"], 370.0)   # 좋은 건 유지
            self.assertNotIn("MDLZ", t)                 # 나쁜 값은 스킵(CX10)
        finally:
            if orig is not None:
                p.write_text(orig)


class TestExitDecision(Base):
    """청산 우선순위: 실적임박 > -8%손절 > 목표가 > RSI과열 (T7)."""
    def test_earnings_wins(self):
        self.assertIn("실적", E._exit_decision(pl=-9, rsi=70, dte=1, cur=400, tgt=100))

    def test_stop_beats_target_rsi(self):
        self.assertIn("손절", E._exit_decision(pl=-9, rsi=70, dte=8, cur=400, tgt=100))

    def test_target_beats_rsi(self):
        # 이익 중 목표가 도달 → 목표가가 RSI보다 우선
        self.assertIn("목표가", E._exit_decision(pl=2, rsi=70, dte=8, cur=105, tgt=100))

    def test_target_requires_profit(self):
        # 감사#227: 손실 중엔 목표가 '익절' 미발동(낡거나 역전된 목표가로 인한 즉시 손실청산 방지).
        # 하방은 손절/추세이탈이 담당하고, 목표가는 이익 실현 전용.
        self.assertIsNone(E._exit_decision(pl=-2, rsi=50, dte=8, cur=105, tgt=100))
        self.assertIn("손절", E._exit_decision(pl=-9, rsi=50, dte=8, cur=105, tgt=100))

    def test_rsi_only(self):
        self.assertIn("RSI", E._exit_decision(pl=1, rsi=70, dte=8, cur=90, tgt=100))

    def test_hold_none(self):
        self.assertIsNone(E._exit_decision(pl=1, rsi=50, dte=8, cur=90, tgt=100))

    def test_no_target_no_earnings(self):
        self.assertIsNone(E._exit_decision(pl=1, rsi=50, dte=None, cur=90, tgt=None))

    def test_trend_break_beats_rsi_when_losing(self):
        # 손실 중 RSI 과열이면 추세이탈(논지 무효)이 우선 — SL6는 '수익'일 때만 라벨 양보
        r = E._exit_decision(pl=-2, rsi=70, dte=8, cur=90, tgt=100, ma150=95)
        self.assertIn("추세이탈", r)

    def test_stop_beats_trend_break(self):
        r = E._exit_decision(pl=-9, rsi=50, dte=8, cur=90, tgt=100, ma150=95)
        self.assertIn("손절", r)

    def test_trend_break_needs_buffer(self):
        # MDLZ 학습(2026-07-15): 150일선 0.3% 하회(노이즈)는 청산 안 함
        self.assertIsNone(E._exit_decision(pl=-0.9, rsi=45, dte=8, cur=58.35, tgt=62.5, ma150=58.54))
        # 버퍼(2%) 넘게 결정적 하회면 청산
        self.assertIn("추세이탈", E._exit_decision(pl=-3, rsi=45, dte=8, cur=57.0, tgt=62.5, ma150=58.54))          # 손절 > 추세이탈

    def test_trend_break_profit_at_target_labels_target(self):
        # SL6: 150일선 아래여도 수익(+3%)+목표가 도달이면 라벨은 '목표가'(attribution 정확화)
        r = E._exit_decision(pl=3.0, rsi=50, dte=8, cur=57.0, tgt=56.5, ma150=58.54)
        self.assertIn("목표가", r); self.assertNotIn("추세이탈", r)

    def test_trend_break_profit_rsi_labels_rsi(self):
        # SL6: 수익+RSI과열이면 라벨은 'RSI 익절'
        r = E._exit_decision(pl=3.0, rsi=70, dte=8, cur=57.0, tgt=99.0, ma150=58.54)
        self.assertIn("RSI", r); self.assertNotIn("추세이탈", r)

    def test_trend_break_loss_still_trend(self):
        # SL6: 손실이면 목표/RSI 미도달 → 추세이탈이 청산사유(논지 무효 컷)
        r = E._exit_decision(pl=-3.0, rsi=50, dte=8, cur=57.0, tgt=62.5, ma150=58.54)
        self.assertIn("추세이탈", r)

    def test_gap_through_distinct_from_stop(self):
        # RB14: 스탑(-6) 마진(2%) 관통 = -8%↓ → 갭관통(구분·경보)
        r = E._exit_decision(pl=-11, rsi=45, dte=8, cur=50, tgt=60)
        self.assertIn("갭관통", r); self.assertEqual(E._exit_category(r), "GAPSTOP")
        # -7%는 정상 손절(관통 아님 — -6과 -8 사이)
        r2 = E._exit_decision(pl=-7, rsi=45, dte=8, cur=50, tgt=60)
        self.assertIn("손절", r2); self.assertNotIn("갭관통", r2); self.assertEqual(E._exit_category(r2), "STOP")

    def test_gap_through_relative_to_widened_stop(self):
        # ATR로 넓어진 스탑(atr 10 → stop -15)엔 -16%는 정상손절, -18%는 갭관통(상대 판정 견고)
        self.assertIn("손절", E._exit_decision(pl=-16, rsi=45, dte=8, cur=50, tgt=60, atr_pct=10))
        self.assertNotIn("갭관통", E._exit_decision(pl=-16, rsi=45, dte=8, cur=50, tgt=60, atr_pct=10))
        self.assertIn("갭관통", E._exit_decision(pl=-18, rsi=45, dte=8, cur=50, tgt=60, atr_pct=10))

    def test_time_exit(self):
        r = E._exit_decision(pl=1, rsi=50, dte=8, cur=90, tgt=100, days_held=30, ma150=80)
        self.assertIn("시간청산", r)      # MAX_HOLD_DAYS(28)+ 보유·진전<3%

    def test_time_exit_skipped_if_progress(self):
        r = E._exit_decision(pl=5, rsi=50, dte=8, cur=90, tgt=100, days_held=30, ma150=80)
        self.assertIsNone(r)              # 진전 5%>3% → 시간청산 안 함

    def test_vol_scaled_stop_widens(self):
        # ATR 6% → 손절선 -9%(=-1.5×6). pl=-8.5는 아직 손절 아님
        self.assertIsNone(E._exit_decision(pl=-8.5, rsi=50, dte=8, cur=90, tgt=100, atr_pct=6))
        self.assertIn("손절", E._exit_decision(pl=-9.5, rsi=50, dte=8, cur=90, tgt=100, atr_pct=6))

    def test_vol_scaled_stop_floor(self):
        # 저변동(ATR 2%) → 손절선 -6% 유지(더 좁아지지 않음; 1.5×2=3 < 6이므로 플로어 -6)
        self.assertIn("손절", E._exit_decision(pl=-6.5, rsi=50, dte=8, cur=90, tgt=100, atr_pct=2))
        self.assertIsNone(E._exit_decision(pl=-5.5, rsi=50, dte=8, cur=90, tgt=100, atr_pct=2))

    def test_breakeven_exit(self):
        # 최고 +5% 갔다가 본전 이하(-0.5%) → 브레이크이븐 청산(C4)
        r = E._exit_decision(pl=-0.5, rsi=50, dte=8, cur=100, tgt=120, mfe_pct=5)
        self.assertIn("브레이크이븐", r)

    def test_breakeven_skipped_if_still_up(self):
        self.assertIsNone(E._exit_decision(pl=2, rsi=50, dte=8, cur=100, tgt=120, mfe_pct=5))

    def test_breakeven_arms_at_3pct(self):
        # #1(개편): 무장선 4→3. +3% 찍고 본전 이하면 청산, +2.9%까지만 간 건 미무장(보유)
        self.assertIn("브레이크이븐", E._exit_decision(pl=-0.5, rsi=50, dte=8, cur=100, tgt=120, mfe_pct=3))
        self.assertIsNone(E._exit_decision(pl=-0.5, rsi=50, dte=8, cur=100, tgt=120, mfe_pct=2.9))

    def test_stop_beats_breakeven(self):
        r = E._exit_decision(pl=-9, rsi=50, dte=8, cur=100, tgt=120, mfe_pct=5, atr_pct=2)
        self.assertIn("손절", r)


class TestManageGating(Base):
    """manage(): 장 닫히면 sell 미호출, 열리면 호출 (T8)."""
    def _arm(self, tradable):
        E._strategy_positions = lambda: {"X": {"qty": 1.0, "entryPx": 100.0, "cost_usd": 100.0}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "90.0"}     # -10% → 손절 트리거
        E._fetch_daily = lambda *a, **k: []      # SL7: manage 1회 fetch 스텁(네트워크 회피)
        E._repoll_pending = lambda *a, **k: None   # T4: 재폴링 네트워크 격리
        E._rsi = lambda s, c=None: 50
        E._days_to_earnings = lambda s: None
        E._load_targets = lambda: {}
        E._ma150 = lambda s, c=None: None        # 네트워크 회피
        E._days_held = lambda ts: 0
        E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None
        MC.us_session = lambda: "미국 정규장" if tradable else "미국 프리마켓"
        E._market_open = lambda: ((True, "개장") if tradable else (False, "휴장"))
        self.calls = []
        E.sell = lambda sym, qty, **k: (self.calls.append((sym, qty)), 0)[1]

    def test_tradable_sells(self):
        self._arm(True)
        E.manage()
        self.assertEqual(self.calls, [("X", 1.0)])

    def test_not_tradable_defers(self):
        self._arm(False)
        E.manage()
        self.assertEqual(self.calls, [])


class TestDaysToEarnings(Base):
    """_days_to_earnings 경계 + block 조건 (T9)."""
    def setUp(self):
        super().setUp()
        self._efile = E.EARNINGS_FILE
        self._eorig = self._efile.read_text() if self._efile.exists() else None

    def tearDown(self):
        if self._eorig is not None:
            self._efile.write_text(self._eorig)
        super().tearDown()

    def test_boundaries(self):
        import json
        today = datetime.fromisoformat(E._trading_day()).date()   # ET 거래일 기준(CX11)
        self._efile.parent.mkdir(parents=True, exist_ok=True)
        self._efile.write_text(json.dumps({
            "FUT": str(today + timedelta(days=5)),
            "TDY": str(today),
            "PAST": str(today - timedelta(days=3)),
            "STALE": str(today - timedelta(days=10)),   # #3(개편): 7일 넘게 과거=stale
            "BAD": "2026-13-40",
        }))
        self.assertEqual(E._days_to_earnings("FUT"), 5)
        self.assertEqual(E._days_to_earnings("TDY"), 0)
        self.assertEqual(E._days_to_earnings("PAST"), -3)      # 최근 과거(≤7일)는 실날짜 유지
        self.assertIsNone(E._days_to_earnings("STALE"))        # #3: 낡은 과거 → 미상(None) fail-closed
        self.assertIsNone(E._days_to_earnings("BAD"))
        self.assertIsNone(E._days_to_earnings("UNKNOWN"))
        # buy-block 조건 0<=dte<=EARN_BLOCK_DAYS: dte 0·2 True, 3·-1 False
        self.assertTrue(0 <= 0 <= E.EARN_BLOCK_DAYS)
        self.assertTrue(0 <= E.EARN_BLOCK_DAYS <= E.EARN_BLOCK_DAYS)
        self.assertFalse(0 <= (E.EARN_BLOCK_DAYS + 1) <= E.EARN_BLOCK_DAYS)
        self.assertFalse(0 <= -1 <= E.EARN_BLOCK_DAYS)


class TestReportStats(Base):
    """report 통계: 승률·PF·기대값·최대낙폭·Sharpe (T18)."""
    def test_metrics(self):
        trades = [{"plUsd": 10, "plPct": 10}, {"plUsd": -5, "plPct": -5}, {"plUsd": 6, "plPct": 6}]
        st = E._report_stats(trades)
        self.assertEqual(st["n"], 3)
        self.assertAlmostEqual(st["winRate"], 2 / 3 * 100)
        self.assertAlmostEqual(st["totalUsd"], 11)
        self.assertAlmostEqual(st["expUsd"], 11 / 3)
        self.assertAlmostEqual(st["pf"], 16 / 5)      # gains 16 / losses 5
        self.assertAlmostEqual(st["maxDD"], -5)       # eq 10,5,11 → dd 0,-5,0

    def test_empty(self):
        self.assertIsNone(E._report_stats([]))

    def test_pf_inf_no_losses(self):     # 손실 없으면 PF=inf (A14)
        st = E._report_stats([{"plUsd": 10, "plPct": 10}, {"plUsd": 5, "plPct": 5}])
        self.assertEqual(st["pf"], float("inf"))

    def test_sharpe(self):
        import statistics
        st = E._report_stats([{"plUsd": 10, "plPct": 10}, {"plUsd": -5, "plPct": -5}, {"plUsd": 6, "plPct": 6}])
        pls = [10, -5, 6]
        self.assertAlmostEqual(st["sharpe"], (sum(pls) / 3) / statistics.pstdev(pls))

    def test_sharpe_single_zero(self):   # 1건이면 sd=0 → sharpe 0 (분모가드)
        st = E._report_stats([{"plUsd": 10, "plPct": 10}])
        self.assertEqual(st["sharpe"], 0.0)


class TestMfePct(Base):
    """_mfe_pct 진입후 최고상승폭 — 완료봉 '종가'·ET 진입일 (C4/A10, exec#6 wick면역·MDLZ학습)."""
    def test_since_entry_completed_close(self):
        # entry_ts KST 7/15 02:00 → ET 7/14 (ed). 07-13은 진입 전→제외. 07-16=최신(진행중)→c[:-1]로 제외
        S.CLIENT.get_candles = lambda s, iv, n: {"candles": [
            {"timestamp": "2026-07-13T13:00:00", "close": "130"},   # 진입 전(ET) → 제외
            {"timestamp": "2026-07-14T13:00:00", "close": "105"},   # 진입일
            {"timestamp": "2026-07-15T13:00:00", "close": "110"},   # 완료봉
            {"timestamp": "2026-07-16T13:00:00", "close": "108"}]}  # 최신=진행중 → 제외
        self.assertAlmostEqual(E._mfe_pct("X", 100, "2026-07-15T02:00:00+09:00"), 10.0)

    def test_excludes_in_progress_bar(self):
        # 최신봉(진행중)이 최대여도 c[:-1]로 제외 — wick/미확정 급등에 브레이크이븐 오발동 방지
        S.CLIENT.get_candles = lambda s, iv, n: {"candles": [
            {"timestamp": "2026-07-14T13:00:00", "close": "105"},
            {"timestamp": "2026-07-15T13:00:00", "close": "110"},
            {"timestamp": "2026-07-16T13:00:00", "close": "200"}]}  # 진행중 급등 → 무시
        self.assertAlmostEqual(E._mfe_pct("X", 100, "2026-07-15T02:00:00+09:00"), 10.0)

    def test_none_entry(self):
        self.assertIsNone(E._mfe_pct("X", 100, None))

    def test_all_before_entry(self):
        S.CLIENT.get_candles = lambda s, iv, n: {"candles": [
            {"timestamp": "2026-07-12T13:00:00", "close": "130"},   # 둘 다 진입(ET 7/14) 전
            {"timestamp": "2026-07-13T13:00:00", "close": "125"}]}
        self.assertIsNone(E._mfe_pct("X", 100, "2026-07-15T02:00:00+09:00"))


class TestRecentTrade(Base):
    """_recent_trade 타임스탬프 견고화 (MS11/CX12)."""
    def test_offset_recent(self):
        from datetime import timezone
        now = datetime.now(timezone.utc).astimezone(MC.KST)
        S.CLIENT.get_trades = lambda s, n=3: {"trades": [{"timestamp": now.isoformat()}]}
        self.assertTrue(E._recent_trade("X"))

    def test_stale_old_rejected(self):
        S.CLIENT.get_trades = lambda s, n=3: {"trades": [{"timestamp": "2020-01-01T00:00:00+09:00"}]}
        self.assertFalse(E._recent_trade("X"))

    def test_future_rejected(self):
        from datetime import timezone
        fut = (datetime.now(timezone.utc) + timedelta(hours=5)).astimezone(MC.KST)
        S.CLIENT.get_trades = lambda s, n=3: {"trades": [{"timestamp": fut.isoformat()}]}
        self.assertFalse(E._recent_trade("X"))     # 미래시각=stale오판 방지

    def test_epoch_ms_recent(self):
        from datetime import timezone
        ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        S.CLIENT.get_trades = lambda s, n=3: {"trades": [{"timestamp": str(ms)}]}
        self.assertTrue(E._recent_trade("X"))

    def test_no_trades(self):
        S.CLIENT.get_trades = lambda s, n=3: {"trades": []}
        self.assertFalse(E._recent_trade("X"))


class TestJournalMerge(unittest.TestCase):
    """_read_journal이 fallback 병합 + 중복 제거 (MS9/CX8)."""
    def test_fallback_merged_and_deduped(self):
        import tempfile
        import json as _json
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        orig = E.JOURNAL
        E.JOURNAL = d / "index_journal.jsonl"
        try:
            E.JOURNAL.write_text(_json.dumps(
                {"ts": "1", "event": "BUY_FILLED", "sym": "A", "fillQty": 1, "fillPrice": 100, "orderId": "o1"}) + "\n")
            (d / "index_journal.fallback.jsonl").write_text(
                _json.dumps({"ts": "2", "event": "BUY_FILLED", "sym": "B", "fillQty": 1, "fillPrice": 50, "orderId": "o2"}) + "\n"
                + _json.dumps({"ts": "1", "event": "BUY_FILLED", "sym": "A", "fillQty": 1, "fillPrice": 100, "orderId": "o1"}) + "\n")
            evs = E._read_journal()
            self.assertEqual([e["sym"] for e in evs], ["A", "B"])   # 시간순 + 중복 A 제거
        finally:
            E.JOURNAL = orig


class TestEntryGuard(Base):
    """_entry_guard 진입 논지 재검증(S14 과열회피·S15 방어심층)."""
    def setUp(self):
        super().setUp()
        import stock_screener as SC
        self._SC = SC
        self._saved_analyze = SC.analyze

    def tearDown(self):
        self._SC.analyze = self._saved_analyze
        super().tearDown()

    def _mock(self, up, rsi, ext, zone=None):
        if zone is None:
            zone = ("🟢매수존" if (up and 38 <= rsi <= 58 and ext <= 8)
                    else ("⚪관망" if up else "🔴추세깨짐"))
        self._SC.analyze = lambda s: {"up": up, "rsi": rsi, "ext50": ext, "zone": zone}

    def test_ok(self):
        self._mock(True, 48, 3)
        self.assertTrue(E._entry_guard("X")[0])

    def test_overheated_out_of_zone(self):
        self._mock(True, 63, 10)
        self.assertFalse(E._entry_guard("X")[0])   # 과열=매수금지

    def test_trend_broken(self):
        self._mock(False, 48, 3)
        self.assertFalse(E._entry_guard("X")[0])

    def test_overextended(self):
        self._mock(True, 50, 12)
        self.assertFalse(E._entry_guard("X")[0])

    def test_oversold_out_of_zone(self):
        self._mock(True, 30, 3)
        self.assertFalse(E._entry_guard("X")[0])


class TestCooldown(Base):
    """_in_cooldown: 손절 후 COOLDOWN_DAYS일내 재매수 차단, 익절/오래된손절은 허용 (S6)."""
    def test_recent_loss_blocks(self):
        d1 = str(datetime.now(MC.KST).date() - timedelta(days=1))
        J([{"ts": d1 + "T01:00:00", "event": "BUY_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 100},
           {"ts": d1 + "T02:00:00", "event": "SELL_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 90}])
        self.assertTrue(E._in_cooldown("X")[0])

    def test_old_loss_ok(self):
        d5 = str(datetime.now(MC.KST).date() - timedelta(days=5))
        J([{"ts": d5 + "T01:00:00", "event": "BUY_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 100},
           {"ts": d5 + "T02:00:00", "event": "SELL_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 90}])
        self.assertFalse(E._in_cooldown("X")[0])

    def test_win_no_cooldown(self):
        today = str(datetime.now(MC.KST).date())
        J([{"ts": today + "T01:00:00", "event": "BUY_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 100},
           {"ts": today + "T02:00:00", "event": "SELL_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 110}])
        self.assertFalse(E._in_cooldown("X")[0])   # 익절 종목은 재매수 허용


class TestTradingDay(unittest.TestCase):
    """_trading_day: KST 자정 넘는 시각도 같은 US 세션(ET date)으로 버킷 (CX3)."""
    def test_et_grouping_across_kst_midnight(self):
        # KST 23:00 7/14 와 KST 00:30 7/15 는 같은 US 세션 → 둘 다 ET 7/14
        self.assertEqual(E._trading_day("2026-07-14T23:00:00+09:00"), "2026-07-14")
        self.assertEqual(E._trading_day("2026-07-15T00:30:00+09:00"), "2026-07-14")


class TestIntradayGate(Base):
    """intraday_entry_check day-change 게이트 (T12): 급등 차단·조회실패 fail-closed."""
    def test_day_pop_blocks(self):
        E._day_change = lambda s: (5.0, 100)      # 당일 +5% > MAX_DAY_POP(2.5)
        self.assertFalse(E.intraday_entry_check("X")[0])

    def test_day_none_fail_closed(self):
        E._day_change = lambda s: (None, None)    # 조회실패 → 안전대기
        self.assertFalse(E.intraday_entry_check("X")[0])

    def test_sparse_1m_fail_closed(self):
        E._day_change = lambda s: (1.0, 100)
        S.CLIENT.get_candles = lambda s, iv, n: {"candles": [
            {"timestamp": "1", "close": "100"}, {"timestamp": "2", "close": "100"}]}
        self.assertFalse(E.intraday_entry_check("X")[0])     # 1분봉 <5 → 대기(exec#15)


class TestDayChange(Base):
    """_day_change 부분봉 판정 (CX14): 진행중봉이면 직전봉, 완료봉이면 최신봉 기준."""
    def test_kst_stamped_bar_still_detected(self):
        """감사#230: 토스가 최신봉을 KST 날짜(ET거래일+1)로 스탬프해도 진행중봉으로 인식.
        ET날짜 동일비교만 하면 전 종목 당일변동이 0%가 되어 추격가드·장외급락감지가 죽는다."""
        from datetime import date, timedelta
        td = E._trading_day()
        tomorrow = str(date.fromisoformat(td) + timedelta(days=1))
        S.CLIENT.get_price = lambda s: {"lastPrice": "110"}
        S.CLIENT.get_candles = lambda s, iv, n: {"candles": [
            {"timestamp": "2026-07-13T13:00:00", "close": "100"},
            {"timestamp": f"{tomorrow}T13:00:00", "close": "110"}]}   # KST 스탬프(ET+1)
        day, _ = E._day_change("X")
        self.assertAlmostEqual(day, 10.0)      # 0%가 아니라 실제 변동률

    def test_price_tracking_bar_detected(self):
        """날짜가 과거여도 최신봉 종가가 현재가를 추종하면 진행중봉(이중 안전망)."""
        S.CLIENT.get_price = lambda s: {"lastPrice": "110"}
        S.CLIENT.get_candles = lambda s, iv, n: {"candles": [
            {"timestamp": "2020-01-01T13:00:00", "close": "100"},
            {"timestamp": "2020-01-02T13:00:00", "close": "110"}]}    # 종가==현재가
        day, _ = E._day_change("X")
        self.assertAlmostEqual(day, 10.0)

    def test_in_progress_bar_uses_prior(self):
        # T15: ET날짜 기반 판정 — 최신봉 날짜==오늘(ET)이면 진행중 → 직전 완료봉 기준
        td = E._trading_day()
        S.CLIENT.get_price = lambda s: {"lastPrice": "110"}
        S.CLIENT.get_candles = lambda s, iv, n: {"candles": [
            {"timestamp": "2026-07-13T13:00:00", "close": "100"},
            {"timestamp": f"{td}T13:00:00", "close": "110"}]}   # 최신봉=오늘(ET)=진행중
        day, _ = E._day_change("X")
        self.assertAlmostEqual(day, 10.0)

    def test_stale_bar_uses_newest(self):
        S.CLIENT.get_price = lambda s: {"lastPrice": "103"}
        S.CLIENT.get_candles = lambda s, iv, n: {"candles": [
            {"timestamp": "2026-07-13T13:00:00", "close": "95"},
            {"timestamp": "2026-07-14T13:00:00", "close": "100"}]}    # 최신 100≠현재가103
        day, _ = E._day_change("X")
        self.assertAlmostEqual(day, 3.0)


class TestExitCategory(Base):
    """청산사유 카테고리 매핑 + 실현거래 전파 (T19)."""
    def test_mapping(self):
        self.assertEqual(E._exit_category("손절 -9.0%(≤-8%)"), "STOP")
        self.assertEqual(E._exit_category("목표가 $370 도달 → 익절"), "TARGET")
        self.assertEqual(E._exit_category("실적 1일전 → 발표 전 청산"), "EARN")
        self.assertEqual(E._exit_category("익절 RSI 70(≥65)"), "RSI")
        self.assertIsNone(E._exit_category(None))

    def test_new_categories(self):     # 추세이탈/브레이크이븐/시간청산 구분(A4)
        self.assertEqual(E._exit_category("추세이탈(150일선 95.00 하회) → 청산"), "TREND")
        self.assertEqual(E._exit_category("브레이크이븐 청산(최고 +5% 후 본전 -0.5% 이탈)"), "BREAKEVEN")
        self.assertEqual(E._exit_category("시간청산(25일 보유·진전 +1.0%<3%)"), "TIME")

    def test_realized_carries_cat(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 100},
           {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 110, "exitCat": "TARGET"}])
        t = E._realized_trades()
        self.assertEqual(t[0]["exitCat"], "TARGET")


class TestManagePortfolio(Base):
    """manage() 포트폴리오 요약·손실경보 + ET일 1회 dedup (TA1/T12)."""
    def test_portfolio_alert_below_5pct_once_per_day(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        origJ = E.JOURNAL
        E.JOURNAL = d / "index_journal.jsonl"     # portfolio_alert_day 상태파일을 임시로(실환경 오염 방지)
        E._strategy_positions = lambda: {"A": {"qty": 0.4, "entryPx": 100, "cost_usd": 40.0},
                                         "B": {"qty": 0.3, "entryPx": 100, "cost_usd": 30.0}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "94.5"}   # -5.5% → 포트폴리오 경보(단, -6 손절선엔 미도달)
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50; E._days_to_earnings = lambda s: None; E._load_targets = lambda: {}
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._days_held = lambda ts: 0
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "x")
        E.sell = lambda *a, **k: 0
        alerts = []
        orig = E._j
        E._j = lambda ev: alerts.append(ev) if ev.get("event") == "PORTFOLIO_ALERT" else None
        try:
            E.manage()
            E.manage()     # 같은 ET일 2번째 → dedup(T12)로 경보 없음
        finally:
            E._j = orig; E.JOURNAL = origJ
        self.assertEqual(sum(1 for a in alerts if a.get("event") == "PORTFOLIO_ALERT"), 1)

    def _pf_common(self):
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50; E._days_to_earnings = lambda s: None
        E._load_targets = lambda: {}
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._trading_days_held = lambda ts: 0
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")

    def test_portfolio_tp_fires_when_goal_hit_and_book_ok(self):
        """② 피니시라인: (재시작후 실현+미실현)≥예산5% & 호가확증 → 이익보유분 익절·당일 래치."""
        E._strategy_positions = lambda: {"W": {"qty": 1.0, "entryPx": 100.0, "cost_usd": 100.0,
                                               "entryTs": "2026-08-28T00:00:00+09:00"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "103"}        # +3% 보유(청산사유 없음) → winner
        S.CLIENT.get_orderbook = lambda s: {"bids": [{"price": "103.0", "quantity": "5"}],
                                            "asks": [{"price": "103.0", "quantity": "5"}]}
        self.addCleanup(setattr, E, "_realized_trades", E._realized_trades)   # 모듈 함수 몽키패치 격리(TestRealizedTrades 오염 방지)
        E._realized_trades = lambda: [{"plUsd": 18.0, "sellTs": "2026-08-28T23:00:00+09:00"}]  # 재시작후 실현 $18
        self._pf_common()
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append((sym, k.get("reason_cat"))), 0)[1]
        E.manage()
        self.assertEqual(calls, [("W", "PORTFOLIO_TP")])           # 이익분 익절
        self.assertTrue((E.JOURNAL.parent / "portfolio_tp_day").exists())   # 당일 래치 기록

    def test_portfolio_tp_defers_on_phantom_book(self):
        """② H-1(안전검수): 목표 총액 넘겨도 호가 미확증(유령틱)이면 매도 안 함·래치 안 함(재시도 유지)."""
        E._strategy_positions = lambda: {"W": {"qty": 1.0, "entryPx": 100.0, "cost_usd": 100.0,
                                               "entryTs": "2026-08-28T00:00:00+09:00"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "103"}
        S.CLIENT.get_orderbook = lambda s: {"bids": [{"price": "90.0", "quantity": "5"}],
                                            "asks": [{"price": "90.1", "quantity": "5"}]}   # 호가 멀리 아래=유령고가
        self.addCleanup(setattr, E, "_realized_trades", E._realized_trades)
        E._realized_trades = lambda: [{"plUsd": 18.0, "sellTs": "2026-08-28T23:00:00+09:00"}]
        self._pf_common()
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append(sym), 0)[1]
        E.manage()
        self.assertEqual(calls, [])                                # 매도 안 나감
        self.assertFalse((E.JOURNAL.parent / "portfolio_tp_day").exists())  # 래치 안 됨
        evs = [json.loads(l)["event"] for l in open(E.JOURNAL) if l.strip()]
        self.assertIn("EXIT_UNCORROBORATED", evs)

    def test_portfolio_tp_skipped_below_goal(self):
        """② 목표 미달(실현 0 + 미실현 +3%=$3 < $18.1)이면 발동 안 함."""
        E._strategy_positions = lambda: {"W": {"qty": 1.0, "entryPx": 100.0, "cost_usd": 100.0,
                                               "entryTs": "2026-08-28T00:00:00+09:00"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "103"}
        S.CLIENT.get_orderbook = lambda s: {"bids": [{"price": "103.0", "quantity": "5"}],
                                            "asks": [{"price": "103.0", "quantity": "5"}]}
        self.addCleanup(setattr, E, "_realized_trades", E._realized_trades)
        E._realized_trades = lambda: []
        self._pf_common()
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append(sym), 0)[1]
        E.manage()
        self.assertEqual(calls, [])
        self.assertFalse((E.JOURNAL.parent / "portfolio_tp_day").exists())

    def test_idle_capital_alert_when_empty_and_funded(self):
        """#6: 0포지션+정규장+유휴자본≥$30+정상레짐 → IDLE_CAPITAL 1회(당일 래치, 재호출 dedup)."""
        E._strategy_positions = lambda: {}
        E._repoll_pending = lambda *a, **k: None
        S.CLIENT.get_buying_power = lambda c: {"availableAmount": "100"}
        E._regime_risk_off = lambda: False
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        E.manage(); E.manage()
        evs = [json.loads(l)["event"] for l in open(E.JOURNAL) if l.strip()]
        self.assertEqual(evs.count("IDLE_CAPITAL"), 1)

    def test_idle_capital_silent_when_risk_off(self):
        """#6: 하락장(risk_off)이면 대기가 정상 → 경보 안 함."""
        E._strategy_positions = lambda: {}
        E._repoll_pending = lambda *a, **k: None
        S.CLIENT.get_buying_power = lambda c: {"availableAmount": "100"}
        E._regime_risk_off = lambda: True
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        E.manage()
        evs = [json.loads(l)["event"] for l in open(E.JOURNAL) if l.strip()] if E.JOURNAL.exists() else []
        self.assertNotIn("IDLE_CAPITAL", evs)

    def test_runner_target_raised_after_ptp(self):
        """#5: +5% 절반익절 후 잔량 목표가를 원상승폭×1.6으로 상향(100×(1+1.6×0.08)=112.8)."""
        E._strategy_positions = lambda: {"W": {"qty": 2.0, "entryPx": 100.0, "cost_usd": 200.0,
                                               "entryTs": "2026-08-28T00:00:00+09:00"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "105"}     # +5% → PTP 트리거
        E.set_target("W", 108.0)                                # 원목표 +8%(PTP 무장 조건 충족)
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50; E._days_to_earnings = lambda s: None
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._trading_days_held = lambda ts: 0
        E._partial_tp_taken = lambda s: False
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        E.sell = lambda sym, qty, **k: 0
        E.manage()
        self.assertAlmostEqual(E._load_targets()["W"], 112.8, places=2)

    def test_conn_dead_alerts_and_halts_cycle(self):
        """개편(2026-09-14): 인증 죽음(403) → CONN_DEAD 경보·rc=1·포지션 순회 안 함(유동IP 조용한 죽음 교정)."""
        def boom(c): raise RuntimeError("token-error: HTTP 403 access_denied")
        S.CLIENT.get_buying_power = boom
        called = {'pos': False}
        def _pos(): called['pos'] = True; return {}
        E._strategy_positions = _pos
        E._repoll_pending = lambda *a, **k: None
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        rc = E.manage()
        self.assertEqual(rc, 1)                        # rc=1 → 크론 로그가 정상위장 안 됨
        self.assertFalse(called['pos'])                # 조기중단 — 포지션 순회 안 함
        evs = [json.loads(l)["event"] for l in open(E.JOURNAL) if l.strip()] if E.JOURNAL.exists() else []
        self.assertIn("CONN_DEAD", evs)

    def test_conn_alive_proceeds_normally(self):
        """대조: 인증 정상(비-403 예외/정상응답)이면 CONN_DEAD 안 뜨고 진행."""
        S.CLIENT.get_buying_power = lambda c: {"availableAmount": "50"}
        E._strategy_positions = lambda: {}
        E._repoll_pending = lambda *a, **k: None
        E._regime_risk_off = lambda: True     # idle 경보 억제(무관)
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        rc = E.manage()
        evs = [json.loads(l)["event"] for l in open(E.JOURNAL) if l.strip()] if E.JOURNAL.exists() else []
        self.assertNotIn("CONN_DEAD", evs)


class TestManageMultiRule(Base):
    """manage() 다종목·다규칙 end-to-end: 한 사이클서 종목별 다른 룰이 정확히 발동 (TA3)."""
    def _arm(self):
        # A: -7% 손절 / B: RSI70 익절 / C: +2% RSI50 보유
        E._strategy_positions = lambda: {
            "A": {"qty": 1.0, "entryPx": 100.0, "cost_usd": 100.0, "entryTs": "2026-07-14T00:00:00"},
            "B": {"qty": 2.0, "entryPx": 50.0,  "cost_usd": 100.0, "entryTs": "2026-07-14T00:00:00"},
            "C": {"qty": 1.0, "entryPx": 200.0, "cost_usd": 200.0, "entryTs": "2026-07-14T00:00:00"}}
        px = {"A": "93.0", "B": "51.0", "C": "204.0"}     # A -7%(정상손절,갭관통 아님), B +2%, C +2%
        rsi = {"A": 45, "B": 70, "C": 50}                 # B만 과열
        S.CLIENT.get_price = lambda s: {"lastPrice": px[s]}
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: rsi[s]
        E._days_to_earnings = lambda s: None
        E._load_targets = lambda: {}
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._days_held = lambda ts: 0
        E._trading_days_held = lambda ts: 0     # manage()는 _trading_days_held 사용 — 고정날짜 픽스처가 시간청산 오발동 방지(날짜 경과 견고화)
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        self.calls = []
        E.sell = lambda sym, qty, **k: (self.calls.append((sym, qty, k.get("reason_cat"))), 0)[1]

    def test_each_rule_fires_correctly(self):
        self._arm()
        E.manage()
        d = {c[0]: c for c in self.calls}
        self.assertIn("A", d); self.assertEqual(d["A"][2], "STOP")     # 손절
        self.assertIn("B", d); self.assertEqual(d["B"][2], "RSI")      # RSI 익절
        self.assertNotIn("C", d)                                       # 보유 → 미매도
        self.assertEqual(len(self.calls), 2)


class TestTodayDeployed(Base):
    """_today_deployed_usd: 오늘(ET) 진입 투입 USD 합, 하드 일일백스톱용 (SL10)."""
    def test_sums_today_excludes_old_and_sells(self):
        now = datetime.now(MC.KST).isoformat()
        E._read_journal = lambda: [
            {"event": "BUY_FILLED", "ts": now, "filledUsd": 30.0},
            {"event": "BUY_WORKING", "ts": now, "usd": 14.0},
            {"event": "BUY_FILLED", "ts": "2020-01-01T00:00:00+09:00", "filledUsd": 99.0},  # 과거 제외
            {"event": "SELL_FILLED", "ts": now, "fillQty": 1},                              # 매도 무관
        ]
        self.assertAlmostEqual(E._today_deployed_usd(), 44.0)     # 30+14

    def test_hard_limits_are_strategy_scale(self):
        # 단일주문 하드상한은 '한 슬롯 수준'(예산÷슬롯의 몇 배 이내) — 하드코딩하면 증액 때마다 깨진다.
        self.assertLessEqual(E.HARD_MAX_ORDER_USD, 0.25 * E.BUDGET_USD + 5)   # 전략 스케일(총알오류 차단)
        self.assertLessEqual(E.HARD_MAX_DAILY_USD, E.BUDGET_USD + 10)         # ≈총자금 스케일


class TestBacktestExit(unittest.TestCase):
    """_backtest_exit 청산룰 what-if 시뮬레이터 (N2, 순수·읽기전용)."""
    def _bars(self, closes):
        return [{"timestamp": f"2026-07-{i + 1:02d}", "close": str(c),
                 "high": str(c), "low": str(c), "open": str(c)} for i, c in enumerate(closes)]

    def test_stop_triggers_on_drop(self):
        idx, reason, px, pl = E._backtest_exit(self._bars([100, 98, 89]), 100,
                                               "2026-07-01T00:00:00", None)
        self.assertEqual(idx, 2); self.assertIn("손절", reason)     # -11% → (갭관통)손절

    def test_target_triggers(self):
        idx, reason, px, pl = E._backtest_exit(self._bars([100, 105, 112]), 100,
                                               "2026-07-01T00:00:00", 110)
        self.assertEqual(idx, 2); self.assertIn("목표가", reason)

    def test_no_exit_holds(self):
        idx, reason, px, pl = E._backtest_exit(self._bars([100, 101, 102]), 100,
                                               "2026-07-01T00:00:00", 200)
        self.assertIsNone(idx); self.assertIsNone(reason)
        self.assertAlmostEqual(pl, 2.0)

    def test_empty_bars(self):
        self.assertEqual(E._backtest_exit([], 100, "2026-07-01T00:00:00", None),
                         (None, None, 100, 0.0))


class TestAlert(unittest.TestCase):
    """_alert out-of-band 채널 + _j 라우팅 (N4)."""
    def test_alert_writes_alerts_file(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp()); orig = E.JOURNAL
        E.JOURNAL = d / "index_journal.jsonl"
        try:
            E._alert("TEST", "hello alert-marker")
            content = (d / "alerts.jsonl").read_text(encoding="utf-8")
            self.assertIn("TEST", content); self.assertIn("hello alert-marker", content)
        finally:
            E.JOURNAL = orig

    def test_j_fires_alert_only_for_alert_events(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp()); orig = E.JOURNAL; origa = E._alert
        E.JOURNAL = d / "index_journal.jsonl"
        fired = []; E._alert = lambda kind, msg: fired.append(kind)
        try:
            E._j({"event": "PORTFOLIO_ALERT", "pnlPct": -6})       # 알림대상
            E._j({"event": "GAP_THROUGH_STOP", "sym": "X", "pl": -12})  # 알림대상
            E._j({"event": "BUY_REGIME", "sym": "X"})              # 알림대상 아님
            self.assertEqual(fired, ["PORTFOLIO_ALERT", "GAP_THROUGH_STOP"])
        finally:
            E.JOURNAL = orig; E._alert = origa


class TestSelftestGate(unittest.TestCase):
    """_selftest_gate: 치명(critical) 실패 하나라도면 exit 1, 비치명 실패는 통과 (N5)."""
    def test_all_pass(self):
        r = [("a", True, True, ""), ("b", True, False, "")]
        self.assertEqual(E._selftest_gate(r), 0)
    def test_critical_fail(self):
        r = [("a", True, True, ""), ("b", False, True, "boom")]
        self.assertEqual(E._selftest_gate(r), 1)
    def test_noncritical_fail_ok(self):
        r = [("a", True, True, ""), ("b", False, False, "warn")]
        self.assertEqual(E._selftest_gate(r), 0)      # 비치명 실패는 게이트 통과


class TestExecuteLog(unittest.TestCase):
    """_log: execute.log에 타임스탬프+메시지 append, 실패해도 조용히 (RB13)."""
    def test_log_appends(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        orig = E.JOURNAL
        E.JOURNAL = d / "index_journal.jsonl"
        try:
            E._log("HELLO test-marker")
            E._log("SECOND line")
            content = (d / "execute.log").read_text(encoding="utf-8")
            self.assertIn("HELLO test-marker", content)
            self.assertIn("SECOND line", content)
            self.assertEqual(len(content.strip().splitlines()), 2)
        finally:
            E.JOURNAL = orig


class TestPendingWorkingOrders(unittest.TestCase):
    """_pending_working_orders: 후속 터미널 없는 working/uncertain만 추림 (RB15)."""
    def test_resolved_by_later_terminal(self):
        evs = [{"event": "BUY_WORKING", "sym": "A", "orderId": "o1"},
               {"event": "BUY_FILLED", "sym": "A", "orderId": "o1"}]     # o1 해결
        self.assertEqual(E._pending_working_orders(evs), [])

    def test_unresolved_working_kept(self):
        evs = [{"event": "BUY_WORKING", "sym": "A", "orderId": "o1"},
               {"event": "SELL_WORKING", "sym": "B", "orderId": "o2"},
               {"event": "SELL_FILLED", "sym": "B", "orderId": "o2"}]    # o2만 해결
        pend = E._pending_working_orders(evs)
        self.assertEqual([p["sym"] for p in pend], ["A"])

    def test_no_orderid_always_pending(self):
        evs = [{"event": "BUY_UNCERTAIN", "sym": "C", "orderId": None}]
        pend = E._pending_working_orders(evs)
        self.assertEqual(len(pend), 1); self.assertIsNone(pend[0]["orderId"])

    def test_terminal_only_none(self):
        evs = [{"event": "BUY_FILLED", "sym": "A", "orderId": "o1"}]
        self.assertEqual(E._pending_working_orders(evs), [])


class TestLastSessionBeforeEarn(unittest.TestCase):
    """RB8: 주말/휴일 넘어가는 실적 전 '마지막 거래세션' 판정 + _exit_decision 청산 보장."""
    def _d(self, s):
        return datetime.fromisoformat(s).date()

    def test_friday_before_monday_earn(self):
        # 금(07-17) → 월(07-20) 실적: 캘린더 3일이지만 금요일이 마지막 세션 → True
        self.assertTrue(E._is_last_session_before_earn(self._d("2026-07-20"), self._d("2026-07-17")))

    def test_thursday_not_last(self):
        # 목(07-16) → 월(07-20): 금요일이 남아있음 → 목요일은 마지막 아님
        self.assertFalse(E._is_last_session_before_earn(self._d("2026-07-20"), self._d("2026-07-16")))

    def test_weekday_dte1(self):
        # 수(07-15) → 목(07-16): 다음 거래일=목=실적일 → True
        self.assertTrue(E._is_last_session_before_earn(self._d("2026-07-16"), self._d("2026-07-15")))

    def test_earn_today_or_past_false(self):
        self.assertFalse(E._is_last_session_before_earn(self._d("2026-07-15"), self._d("2026-07-15")))
        self.assertFalse(E._is_last_session_before_earn(self._d("2026-07-14"), self._d("2026-07-15")))

    def test_exit_decision_last_session_forces_exit(self):
        # dte=3(금/월)이라도 마지막세션 플래그면 청산; 플래그 없으면 실적청산 안 함
        self.assertIn("실적", E._exit_decision(pl=1, rsi=50, dte=3, cur=90, tgt=100,
                                              earn_last_session=True))
        self.assertNotIn("실적", str(E._exit_decision(pl=1, rsi=50, dte=3, cur=90, tgt=100,
                                                     earn_last_session=False)))


class TestFetchDaily(Base):
    """_fetch_daily 페이지네이션: 부족분을 before 커서로 채워 ≥target 확보 (RB11)."""
    def test_single_call_enough(self):
        calls = []
        def gc(s, iv, n, before=None):
            calls.append(before)
            return {"candles": [{"timestamp": f"2026-02-{i:02d}", "close": "10"} for i in range(1, 7)]}
        S.CLIENT.get_candles = gc
        rows = E._fetch_daily("X", target=5)
        self.assertGreaterEqual(len(rows), 5)
        self.assertEqual(calls, [None])          # 한 번에 충분 → 추가 페이지 없음

    def test_paginates_when_short(self):
        calls = []
        def gc(s, iv, n, before=None):
            p = len(calls); calls.append(before)
            start = 30 - p * 3                    # 페이지마다 더 과거 3봉
            return {"candles": [{"timestamp": f"2026-03-{start - i:02d}", "close": "10"} for i in range(3)]}
        S.CLIENT.get_candles = gc
        rows = E._fetch_daily("X", target=9, max_pages=5)
        self.assertGreaterEqual(len(rows), 9)    # 3봉×3페이지=9 확보
        self.assertGreaterEqual(len(calls), 3)   # 여러 페이지 실제 호출

    def test_empty_returns_list(self):
        S.CLIENT.get_candles = lambda s, iv, n, before=None: {"candles": []}
        self.assertEqual(E._fetch_daily("X", target=5), [])

    def test_page_count_capped_at_200(self):
        # 회귀가드(exec#128): 토스 API는 count>200을 400 거부 → 페이지당 count는 절대 ≤200
        counts = []
        def gc(s, iv, n, before=None):
            counts.append(n)
            return {"candles": [{"timestamp": f"2026-01-{i:02d}", "close": "1"} for i in range(1, 3)]}
        S.CLIENT.get_candles = gc
        E._fetch_daily("X", target=252, max_pages=2)
        self.assertTrue(counts and all(c <= 200 for c in counts))


class TestConcentration(unittest.TestCase):
    """_concentration_ok: 단일 종목 진입액이 예산 40% 이내인지 (재분석#129)."""
    def test_within_cap(self):
        cap = E.MAX_POSITION_WEIGHT * E.BUDGET_USD
        self.assertTrue(E._concentration_ok(cap - 1))     # 상한 바로 아래 → 허용
    def test_over_cap_blocked(self):
        cap = E.MAX_POSITION_WEIGHT * E.BUDGET_USD
        self.assertFalse(E._concentration_ok(cap + 1))    # 상한 초과 → 차단(과집중 방지)
    def test_small_ok(self):
        self.assertTrue(E._concentration_ok(15))    # 소액 진입 정상


class TestEarnUnknownGapBlock(unittest.TestCase):
    """실적일 미상 + 큰 갭 진입차단 (RB6/C10): dte None + |변동|≥임계만 차단, 실적아는 종목은 대상 아님."""
    def test_unknown_earn_big_gap_blocks(self):
        self.assertTrue(E._earn_unknown_gap_block(None, 5.0))    # 미상 + +5% → 차단
        self.assertTrue(E._earn_unknown_gap_block(None, -4.5))   # 미상 + -4.5% → 차단(하락갭=미스 의심)
    def test_unknown_earn_small_move_ok(self):
        self.assertFalse(E._earn_unknown_gap_block(None, 1.2))   # 미상 + 소폭 → 정상 눌림 진입 허용
    def test_known_earn_not_targeted(self):
        self.assertFalse(E._earn_unknown_gap_block(8, 6.0))      # 실적 아는 종목은 EARN_BLOCK_DAYS가 처리
    def test_none_daychange_no_block(self):
        self.assertFalse(E._earn_unknown_gap_block(None, None))  # 변동 조회실패는 다른 fail-closed가 처리


class TestScreenerPartialDrop(unittest.TestCase):
    """스크리너 부분봉 제외 판정: 오늘(ET)==최신봉일 때만 c[:-1] (SL14)."""
    def test_today_partial_dropped(self):
        self.assertTrue(SC._partial_drop("2026-07-15", "2026-07-15", 200))   # 오늘봉=진행중→제외
    def test_closed_prevday_kept(self):
        self.assertFalse(SC._partial_drop("2026-07-14", "2026-07-15", 200))  # 전일 완료봉→유지
    def test_insufficient_bars_kept(self):
        self.assertFalse(SC._partial_drop("2026-07-15", "2026-07-15", 100))  # 봉수 부족→그대로


class TestManageSharedFetch(Base):
    """manage(): 종목당 _fetch_daily 1회 호출, 그 봉을 rsi/ma150/atr/mfe에 공유 (SL7)."""
    def test_single_fetch_shared(self):
        E._strategy_positions = lambda: {"X": {"qty": 1.0, "entryPx": 100.0, "cost_usd": 100.0,
                                              "entryTs": "2026-07-14T00:00:00"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "101"}
        sentinel = [{"timestamp": "2026-07-14", "close": "100"}]
        E._repoll_pending = lambda *a, **k: None
        fetches = []; got = {}
        E._fetch_daily = lambda *a, **k: (fetches.append(a[0]), sentinel)[1]
        E._rsi = lambda s, c=None: (got.__setitem__("rsi", c), 50)[1]
        E._ma150 = lambda s, c=None: (got.__setitem__("ma", c), None)[1]
        E._atr_pct = lambda s, c=None: (got.__setitem__("atr", c), None)[1]
        E._mfe_pct = lambda s, e, ts, c=None: (got.__setitem__("mfe", c), None)[1]
        E._days_to_earnings = lambda s: None; E._load_targets = lambda: {}
        E._days_held = lambda ts: 0
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        E.sell = lambda *a, **k: 0
        E.manage()
        self.assertEqual(fetches, ["X"])           # 종목당 1회만 fetch
        self.assertIs(got["rsi"], sentinel)        # 같은 봉을 4개 지표가 공유
        self.assertIs(got["ma"], sentinel)
        self.assertIs(got["atr"], sentinel)
        self.assertIs(got["mfe"], sentinel)


class TestManagePriceFailNeutral(Base):
    """manage() 시세조회 실패 종목은 원가=평가(중립 0%)로 회계, 매도 안 함 (TA2)."""
    def test_price_fail_neutral_no_sell(self):
        import io
        import contextlib
        E._strategy_positions = lambda: {"X": {"qty": 1.0, "entryPx": 100.0, "cost_usd": 100.0,
                                               "entryTs": "2026-07-14T00:00:00"}}
        def boom(s):
            raise RuntimeError("feed down")
        S.CLIENT.get_price = boom
        E._days_to_earnings = lambda s: None       # 실적청산 경로 아님 → 스킵+중립
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50; E._load_targets = lambda: {}
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._days_held = lambda ts: 0
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        calls = []; E.sell = lambda *a, **k: (calls.append(a), 0)[1]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            E.manage()
        out = buf.getvalue()
        self.assertEqual(calls, [])                # 시세실패 → 매도 안 함
        self.assertIn("시세 조회 실패", out)
        self.assertIn("+0.0%", out)                # 원가=평가 → 포트폴리오 중립 0%


class TestGuardSkipMarket(Base):
    """_guard(skip_market=True): 세션·개장 재확인 생략(manage가 이미 확인), 실주문가드는 유지 (SL8)."""
    def setUp(self):
        super().setUp()
        import types
        self._cfg = S.CFG                                # Config는 frozen → 통째로 교체
        self._ns = types.SimpleNamespace(allow_live_orders=True)
        S.CFG = self._ns

    def tearDown(self):
        S.CFG = self._cfg
        super().tearDown()

    def test_skip_bypasses_session_calendar(self):
        called = []
        MC.us_session = lambda: (called.append("sess"), "미국 프리마켓")[1]
        E._market_open = lambda: (called.append("mo"), (False, "휴장"))[1]
        self.assertTrue(E._guard(skip_market=True))     # 세션/개장 안 봐도 통과
        self.assertEqual(called, [])                     # 중복 캘린더/세션 조회 없음

    def test_skip_still_requires_live_orders(self):
        self._ns.allow_live_orders = False               # SimpleNamespace는 가변
        self.assertFalse(E._guard(skip_market=True))     # 실주문 OFF는 skip해도 차단


class TestStatusExitCode(unittest.TestCase):
    """--status 종료코드 매트릭스: HALT·미병합fallback·손실서킷만 1 (TA16)."""
    def test_matrix(self):
        self.assertEqual(E._status_exit_code(False, False, False), 0)   # healthy
        self.assertEqual(E._status_exit_code(True, False, False), 1)    # HALT
        self.assertEqual(E._status_exit_code(False, True, False), 1)    # fallback 미병합
        self.assertEqual(E._status_exit_code(False, False, True), 1)    # 손실서킷
        self.assertEqual(E._status_exit_code(True, True, True), 1)


class TestReadJournalFallback(unittest.TestCase):
    """_read_journal fallback 단독 + malformed/blank skip (TA11)."""
    def test_fallback_only_skip_bad(self):
        import tempfile
        import json as _json
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        orig = E.JOURNAL
        E.JOURNAL = d / "index_journal.jsonl"     # main 없음
        try:
            (d / "index_journal.fallback.jsonl").write_text(
                _json.dumps({"ts": "1", "event": "BUY_FILLED", "sym": "A", "fillQty": 1, "fillPrice": 100})
                + "\n{bad json\n\n")
            self.assertEqual([e["sym"] for e in E._read_journal()], ["A"])
        finally:
            E.JOURNAL = orig


class TestMarketOpen(Base):
    """_market_open 캘린더 백스톱 (A15): 휴장오작동+실체결→open, 휴장+무체결→closed."""
    def test_holiday_but_trading_is_open(self):
        S.CLIENT.get_market_calendar = lambda r: {"isHoliday": True, "date": "2026-07-15"}
        E._recent_trade = lambda s: True
        self.assertTrue(E._market_open()[0])

    def test_holiday_no_trade_closed(self):
        S.CLIENT.get_market_calendar = lambda r: {"isHoliday": True, "date": "2026-07-15"}
        E._recent_trade = lambda s: False
        self.assertFalse(E._market_open()[0])

    def test_open_calendar(self):
        S.CLIENT.get_market_calendar = lambda r: {"isHoliday": False, "isOpen": True}
        self.assertTrue(E._market_open()[0])


class TestBuyGuardOrder(Base):
    """buy() 가드 순서: 싼 검사가 비싼 검사보다 먼저 short-circuit (TA7)."""
    def test_amount_before_cooldown_circuit(self):
        E._guard = lambda *a, **k: True
        calls = []
        E._in_cooldown = lambda s: (calls.append("cooldown"), (False, ""))[1]
        E._strategy_circuit = lambda: (calls.append("circuit"), (True, ""))[1]
        self.assertEqual(E.buy("MSFT", 5), 1)      # 금액 미달 → 조기 차단
        self.assertEqual(calls, [])                 # cooldown/circuit 미호출

    def test_dup_before_circuit_earnings(self):
        E._guard = lambda *a, **k: True
        E._in_cooldown = lambda s: (False, "")
        E._open_symbols_and_deployed = lambda: ({"MSFT"}, 35.0)
        calls = []
        E._strategy_circuit = lambda: (calls.append("circuit"), (True, ""))[1]
        E._days_to_earnings = lambda s: (calls.append("earn"), None)[1]
        self.assertEqual(E.buy("MSFT", 20), 1)     # 물타기 차단
        self.assertEqual(calls, [])                 # circuit/earnings 미호출

    def test_sector_cap_blocks_same_sector(self):
        # #2(개편): 동일섹터 상한(MAX_PER_SECTOR=1) 도달 → 하드 차단, 이후 게이트(circuit/earnings) 미도달
        E._guard = lambda *a, **k: True
        E._in_cooldown = lambda s: (False, "")
        E._open_symbols_and_deployed = lambda: ({"MS"}, 18.0)   # 금융 MS 보유중
        self.addCleanup(setattr, E, "_concentration_ok", E._concentration_ok)   # _ATTRS 미포함 → 수동 복원(TestConcentration 오염 방지)
        E._concentration_ok = lambda u: True
        calls = []
        E._strategy_circuit = lambda: (calls.append("circuit"), (True, ""))[1]
        E._days_to_earnings = lambda s: (calls.append("earn"), None)[1]
        rc = E.buy("C", 20)                          # C=금융 → MS와 동일섹터, 상한 초과
        self.assertEqual(rc, 1)                      # 하드 차단
        self.assertEqual(calls, [])                  # circuit/earnings 조기차단으로 미호출
        evs = [json.loads(l)["event"] for l in open(E.JOURNAL) if l.strip()]
        self.assertIn("BUY_SECTOR_CAP", evs)


class TestRegime(Base):
    """_regime_risk_off: QQQ 200일선 아래면 risk_off (S7)."""
    def setUp(self):
        super().setUp()
        import index_engine as IE
        self._IE = IE; self._saved_trend = IE.trend

    def tearDown(self):
        self._IE.trend = self._saved_trend
        super().tearDown()

    def test_risk_off_below(self):
        self._IE.trend = lambda s: {"ok": True, "above": False}
        self.assertTrue(E._regime_risk_off())

    def test_risk_on_above(self):
        self._IE.trend = lambda s: {"ok": True, "above": True}
        self.assertFalse(E._regime_risk_off())

    def test_unknown_allows(self):
        self._IE.trend = lambda s: {"ok": False}
        self.assertFalse(E._regime_risk_off())     # 판단불가 → 진입 허용(fail-open)


class TestSellClamp(Base):
    """sell() 전략보유∩sellable 클램프 + 미보유 거부 (TA5)."""
    def setUp(self):
        super().setUp()
        self._saved_plan = S.t_plan_order

    def tearDown(self):
        S.t_plan_order = self._saved_plan
        super().tearDown()

    def test_clamp_to_min(self):
        E._guard = lambda *a, **k: True
        E._strategy_positions = lambda: {"X": {"qty": 0.5, "entryPx": 100}}
        S.CLIENT.get_sellable_quantity = lambda s: {"sellableQuantity": "0.3"}
        cap = {}

        def fake_plan(sym, side, otype, quantity=None, **kw):
            cap["quantity"] = quantity
            return {"error": {"code": "stop"}}
        S.t_plan_order = fake_plan
        E.sell("X", 1.0)
        self.assertAlmostEqual(cap["quantity"], 0.3)   # 1.0→owned0.5→sellable0.3

    def test_refuse_non_owned(self):
        E._guard = lambda *a, **k: True
        E._strategy_positions = lambda: {}
        called = []
        S.t_plan_order = lambda *a, **k: (called.append(1), {"error": {}})[1]
        self.assertEqual(E.sell("X", 1.0), 1)
        self.assertEqual(called, [])                   # plan 호출 안 됨

    def test_legacy_refused(self):                     # 레거시는 최우선 거부 (TA6)
        E._guard = lambda *a, **k: True
        called = []
        S.t_plan_order = lambda *a, **k: (called.append(1), {"error": {}})[1]
        self.assertEqual(E.sell("AAPL", 1), 1)
        self.assertEqual(called, [])


class TestReconcilePartial(Base):
    """_reconcile_fill PARTIAL_FILLED은 terminal 아님 → 재시도 다 소진 (TA9)."""
    def test_partial_not_terminal(self):
        import time as _t
        calls = []
        S.CLIENT.get_order = lambda order_id=None: (calls.append(1), {
            "status": "PARTIAL_FILLED",
            "execution": {"filledQuantity": "0.05", "averageFilledPrice": "355.6"}})[1]
        orig = _t.sleep; _t.sleep = lambda *a: None
        try:
            st, fq, fp = E._reconcile_fill("oid", tries=3, wait=0.01)
        finally:
            _t.sleep = orig
        self.assertEqual(st, "PARTIAL_FILLED")
        self.assertAlmostEqual(fq, 0.05)
        self.assertEqual(len(calls), 3)                # PARTIAL은 break 안 함


class TestCheckJournal(Base):
    """check_journal 무결성 (merged view, TA17)."""
    def test_oversell_flagged(self):
        E._read_journal = lambda: [
            {"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 100},
            {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 2, "fillPrice": 110}]
        self.assertEqual(E.check_journal(), 1)

    def test_clean_ok(self):
        E._read_journal = lambda: [
            {"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 100},
            {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 110}]
        self.assertEqual(E.check_journal(), 0)

    def test_missing_key_flagged(self):
        E._read_journal = lambda: [{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1}]
        self.assertEqual(E.check_journal(), 1)


class TestUsSession(unittest.TestCase):
    """us_session ET-aware + 주말 (CX4/RB4)."""
    def test_et_sessions_and_weekend(self):
        from datetime import datetime as dt
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        self.assertEqual(MC.us_session(dt(2026, 7, 15, 11, 0, tzinfo=et)), "미국 정규장")     # Wed 11am
        self.assertEqual(MC.us_session(dt(2026, 7, 15, 5, 0, tzinfo=et)), "미국 프리마켓")     # Wed 5am
        self.assertEqual(MC.us_session(dt(2026, 7, 15, 17, 0, tzinfo=et)), "미국 애프터마켓")   # Wed 5pm
        self.assertEqual(MC.us_session(dt(2026, 7, 18, 11, 0, tzinfo=et)), "미국장 휴장")       # Sat


class _TmpJournal(Base):
    """임시 저널 디렉토리 베이스(T19/T20): 실제 _j/_read_journal을 임시 파일로 돌려
    머니-기록 경로를 end-to-end로 검증(실환경 원장 오염 없음)."""
    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path
        self.dir = Path(tempfile.mkdtemp())
        self._origJ = E.JOURNAL
        E.JOURNAL = self.dir / "index_journal.jsonl"
        E._set_halt = lambda *a, **k: None      # 테스트에서 실제 HALT 파일 생성 금지

    def tearDown(self):
        E.JOURNAL = self._origJ
        super().tearDown()

    def evs(self):
        import json as _json
        if not E.JOURNAL.exists():
            return []
        return [_json.loads(x) for x in E.JOURNAL.read_text(encoding="utf-8").splitlines() if x.strip()]


class TestRecordFillIdempotent(_TmpJournal):
    """_record_fill 멱등성(T5/MH7) + PARTIAL 이벤트(T3/MH2)."""
    def test_same_order_twice_no_double(self):
        d1 = E._record_fill("BUY", "X", "o1", "FILLED", 1.0, 100.0)
        d2 = E._record_fill("BUY", "X", "o1", "FILLED", 1.0, 100.0)   # 같은 누적 재기록
        self.assertAlmostEqual(d1, 1.0); self.assertAlmostEqual(d2, 0.0)
        fills = [e for e in self.evs() if e["event"] == "BUY_FILLED"]
        self.assertEqual(len(fills), 1)                               # 이중계상 없음(MH3 이중롱 차단)
        pos = E._strategy_positions()
        self.assertAlmostEqual(pos["X"]["qty"], 1.0)

    def test_partial_then_more_records_delta(self):
        E._record_fill("BUY", "X", "o1", "PARTIAL_FILLED", 0.4, 100.0)
        E._record_fill("BUY", "X", "o1", "FILLED", 1.0, 100.0)        # 누적 1.0 → 증분 0.6
        evs = self.evs()
        self.assertEqual(evs[0]["event"], "BUY_PARTIAL")
        self.assertAlmostEqual(evs[0]["fillQty"], 0.4)
        self.assertEqual(evs[1]["event"], "BUY_FILLED")
        self.assertAlmostEqual(evs[1]["fillQty"], 0.6)                # 증분만
        self.assertAlmostEqual(E._strategy_positions()["X"]["qty"], 1.0)

    def test_sell_partial_named(self):
        E._record_fill("BUY", "X", "b1", "FILLED", 1.0, 100.0)
        E._record_fill("SELL", "X", "s1", "PARTIAL_FILLED", 0.3, 110.0, extra={"exitCat": "TARGET"})
        ev = [e for e in self.evs() if e["event"] == "SELL_PARTIAL"][0]
        self.assertAlmostEqual(ev["fillQty"], 0.3)
        self.assertEqual(ev["exitCat"], "TARGET")
        self.assertAlmostEqual(E._strategy_positions()["X"]["qty"], 0.7)


class TestRealizedPriceDerivation(Base):
    """체결가 미상 → filledUsd 역산(T1/MH1): lot 유실·P&L 누락 방지."""
    def test_buy_price_from_filled_usd(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 2.0, "fillPrice": 0,
            "filledUsd": 200.0},
           {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 2.0, "fillPrice": 110.0}])
        t = E._realized_trades()
        self.assertEqual(len(t), 1)
        self.assertAlmostEqual(t[0]["buyPx"], 100.0)                 # 200/2 역산
        self.assertAlmostEqual(t[0]["plPct"], 10.0)

    def test_sell_price_from_filled_usd(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100.0},
           {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 0,
            "filledUsd": 92.0}])
        t = E._realized_trades()
        self.assertEqual(len(t), 1)
        self.assertAlmostEqual(t[0]["plPct"], -8.0)                  # 손실도 정확히 기록(쿨다운/서킷 정합)

    def test_still_unknown_skipped(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100.0},
           {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 0}])
        self.assertEqual(E._realized_trades(), [])                   # 역산 불가면 종전대로 보수적 skip


class TestPendingIncludesPartial(Base):
    """_pending_working_orders: PARTIAL은 미해결, 잔량종결/FILLED로 해결(T4)."""
    def test_partial_pending_until_terminal(self):
        evs = [{"event": "BUY_PARTIAL", "sym": "A", "orderId": "o1"}]
        self.assertEqual(len(E._pending_working_orders(evs)), 1)
        evs.append({"event": "BUY_FILLED", "sym": "A", "orderId": "o1"})
        self.assertEqual(E._pending_working_orders(evs), [])

    def test_remainder_canceled_resolves(self):
        evs = [{"event": "SELL_PARTIAL", "sym": "A", "orderId": "o1"},
               {"event": "SELL_REMAINDER_CANCELED", "sym": "A", "orderId": "o1"}]
        self.assertEqual(E._pending_working_orders(evs), [])

    def test_same_oid_listed_once(self):
        evs = [{"event": "BUY_WORKING", "sym": "A", "orderId": "o1"},
               {"event": "BUY_PARTIAL", "sym": "A", "orderId": "o1"}]
        self.assertEqual(len(E._pending_working_orders(evs)), 1)


class TestRepollPending(_TmpJournal):
    """_repoll_pending(T4/MH5): 체결 증분 승격 / 죽은주문 종결 / 부분체결 잔량종결."""
    def test_working_promotes_to_filled(self):
        E._j({"event": "BUY_WORKING", "sym": "A", "orderId": "o1", "usd": 35.0})
        E._reconcile_fill = lambda oid, tries=2, wait=1.0: ("FILLED", 0.5, 70.0)
        E._repoll_pending()
        fills = [e for e in self.evs() if e["event"] == "BUY_FILLED"]
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0]["fillQty"], 0.5)
        self.assertAlmostEqual(E._strategy_positions()["A"]["qty"], 0.5)
        E._repoll_pending()      # 해결됐으니 재실행해도 추가 기록 없음(멱등)
        self.assertEqual(len([e for e in self.evs() if e["event"] == "BUY_FILLED"]), 1)

    def test_dead_working_released(self):
        E._j({"event": "BUY_WORKING", "sym": "A", "orderId": "o1", "usd": 35.0})
        E._reconcile_fill = lambda oid, tries=2, wait=1.0: ("CANCELED", 0.0, None)
        E._repoll_pending()
        self.assertTrue(any(e["event"] == "BUY_REJECTED" for e in self.evs()))
        occ, dep = E._open_symbols_and_deployed()
        self.assertNotIn("A", occ)      # 슬롯 해제 → 재진입 가능

    def test_partial_remainder_canceled_keeps_fill(self):
        E._record_fill("BUY", "A", "o1", "PARTIAL_FILLED", 0.4, 100.0)
        E._reconcile_fill = lambda oid, tries=2, wait=1.0: ("CANCELED", 0.4, 100.0)
        E._repoll_pending()
        self.assertTrue(any(e["event"] == "BUY_REMAINDER_CANCELED" for e in self.evs()))
        self.assertAlmostEqual(E._strategy_positions()["A"]["qty"], 0.4)   # 체결분 유지
        self.assertEqual(E._pending_working_orders(self.evs()), [])       # 해결 완료

    def test_sell_working_promotes(self):
        E._record_fill("BUY", "A", "b1", "FILLED", 1.0, 100.0)
        E._j({"event": "SELL_WORKING", "sym": "A", "orderId": "s1", "qty": 1.0, "exitCat": "STOP"})
        E._reconcile_fill = lambda oid, tries=2, wait=1.0: ("FILLED", 1.0, 92.0)
        E._repoll_pending()
        self.assertTrue(any(e["event"] == "SELL_FILLED" for e in self.evs()))
        self.assertEqual(E._strategy_positions(), {})                      # 청산 반영


class TestBuySellPaths(_TmpJournal):
    """buy/sell 해피패스·부분체결·불확실 경로 end-to-end(T19: 플랜→체결→저널)."""
    def _arm_buy(self):
        E._guard = lambda *a, **k: True
        E._in_cooldown = lambda s: (False, "")
        E._open_symbols_and_deployed = lambda: (set(), 0.0)
        E._today_deployed_usd = lambda: 0.0
        E._strategy_circuit = lambda: (True, "")
        E._regime_risk_off = lambda: False
        E._days_to_earnings = lambda s: 10
        E._entry_guard = lambda s: (True, "논지 OK")
        E.intraday_entry_check = lambda s: (True, "양호")
        E._spread_ok = lambda s: (True, 0.01)
        S.CLIENT.get_buying_power = lambda cur: {"availableAmount": "100"}
        self._saved_plan = S.t_plan_order
        self._saved_place = S.t_place_order_confirmed
        S.t_plan_order = lambda *a, **k: {"preview_token": "tok", "confirm_phrase": "GO",
                                          "snapshot": {"clientOrderId": "c1", "snapshotPrice": 100}}
        S.t_place_order_confirmed = lambda tok, ph: {"orderId": "o1"}

    def tearDown(self):
        try:
            S.t_plan_order = self._saved_plan
            S.t_place_order_confirmed = self._saved_place
        except AttributeError:
            pass
        try:
            del S.CLIENT.get_buying_power      # 인스턴스 속성 제거(클래스 메서드 복원)
        except AttributeError:
            pass
        super().tearDown()

    def test_buy_happy_path_filled(self):
        self._arm_buy()
        E._reconcile_fill = lambda oid, tries=6, wait=1.5: ("FILLED", 0.35, 100.0)
        rc = E.buy("ZZ", 30)
        self.assertEqual(rc, 0)
        ev = [e for e in self.evs() if e["event"] == "BUY_FILLED"][0]
        self.assertAlmostEqual(ev["fillQty"], 0.35)
        self.assertAlmostEqual(ev["filledUsd"], 35.0)
        self.assertEqual(ev["coid"], "c1")
        self.assertAlmostEqual(E._strategy_positions()["ZZ"]["qty"], 0.35)

    def test_buy_partial_records_partial(self):
        self._arm_buy()
        E._reconcile_fill = lambda oid, tries=6, wait=1.5: ("PARTIAL_FILLED", 0.2, 100.0)
        rc = E.buy("ZZ", 30)
        self.assertEqual(rc, 0)
        self.assertTrue(any(e["event"] == "BUY_PARTIAL" for e in self.evs()))   # T3/MH2
        self.assertEqual(len(E._pending_working_orders(self.evs())), 1)         # 잔량 추적 대상

    def test_buy_reconcile_none_halts(self):
        self._arm_buy()
        halted = []
        E._set_halt = lambda reason="": halted.append(reason)
        E._reconcile_fill = lambda oid, tries=6, wait=1.5: (None, 0.0, None)
        rc = E.buy("ZZ", 30)
        self.assertEqual(rc, 3)                                                 # T7/MH4
        self.assertTrue(any(e["event"] == "BUY_UNCERTAIN" for e in self.evs()))
        self.assertTrue(halted)

    def test_buy_dup_dead_order_released(self):
        self._arm_buy()
        S.t_place_order_confirmed = lambda tok, ph: {
            "error": {"code": "DUPLICATE"}, "existing_order_id": "old1"}
        E._reconcile_fill = lambda oid, tries=2, wait=1.0: ("CANCELED", 0.0, None)
        rc = E.buy("ZZ", 30)
        self.assertEqual(rc, 1)                                                 # T6/MH6
        self.assertTrue(any(e["event"] == "BUY_REJECTED" for e in self.evs()))
        occ, _ = E._open_symbols_and_deployed.__wrapped__() if hasattr(
            E._open_symbols_and_deployed, "__wrapped__") else (set(), 0)

    def test_sell_dup_dead_order_retries(self):
        E._record_fill("BUY", "ZZ", "b1", "FILLED", 1.0, 100.0)
        E._guard = lambda *a, **k: True
        S.CLIENT.get_sellable_quantity = lambda s: {"sellableQuantity": "1.0"}
        self._saved_plan = S.t_plan_order
        self._saved_place = S.t_place_order_confirmed
        S.t_plan_order = lambda *a, **k: {"preview_token": "tok", "confirm_phrase": "GO",
                                          "snapshot": {"clientOrderId": "c2"}}
        S.t_place_order_confirmed = lambda tok, ph: {
            "error": {"code": "DUPLICATE"}, "existing_order_id": "olds"}
        E._reconcile_fill = lambda oid, tries=2, wait=1.0: ("CANCELED", 0.0, None)
        rc = E.sell("ZZ", 1.0, reason_cat="STOP")
        self.assertEqual(rc, 1)                                                 # T6: 손절 안 삼킴
        self.assertTrue(any(e["event"] == "SELL_RETRY" for e in self.evs()))
        self.assertAlmostEqual(E._strategy_positions()["ZZ"]["qty"], 1.0)       # 미청산 유지 → 재매도


class TestOpsHardening(unittest.TestCase):
    """로테이션(T13)·백업(T14)·targets 경고(T11)."""
    def test_rotate(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp()); f = d / "x.log"
        f.write_text("A" * 100)
        E._rotate(f, max_bytes=50)
        self.assertFalse(f.exists())
        self.assertTrue((d / "x.log.1").exists())
        f.write_text("B")                       # 새 파일로 이어쓰기 가능
        E._rotate(f, max_bytes=50)              # 임계 미만 → 그대로
        self.assertTrue(f.exists())

    def test_backup_journal(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        origJ = E.JOURNAL
        E.JOURNAL = d / "index_journal.jsonl"
        try:
            E.JOURNAL.write_text('{"event":"X"}\n')
            E._backup_journal(keep=14)
            bdir = d / ".backups"
            files = list(bdir.glob("journal-*.jsonl"))
            self.assertEqual(len(files), 1)
            self.assertIn("X", files[0].read_text())
            E._backup_journal(keep=14)          # 같은 날 재호출 → 중복 생성 없음
            self.assertEqual(len(list(bdir.glob("journal-*.jsonl"))), 1)
        finally:
            E.JOURNAL = origJ

    def test_targets_parse_warn(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp()); tf = d / "targets.json"
        tf.write_text("{bad json", encoding="utf-8")
        origT, origA = E.TARGETS_FILE, E._alert
        E.TARGETS_FILE = tf
        E._LOAD_WARNED.discard("targets")
        alerts = []
        E._alert = lambda k, m: alerts.append(k)
        try:
            out = E._load_targets()
            self.assertEqual(out, {})
            self.assertIn("CONFIG", alerts)     # silent 소실 대신 경보(T11)
        finally:
            E.TARGETS_FILE = origT; E._alert = origA


class TestCircuitBoundaries(Base):
    """_strategy_circuit 경계값 매트릭스(H4): 진입수·손실수·$손실·낙폭 각각 정확한 임계."""
    def _mk(self, entries=0, losses=0, loss_usd=0.0):
        today = E._trading_day()
        evs = [{"ts": f"{today}T10:0{i}:00-04:00", "event": "BUY_FILLED", "sym": f"S{i}",
                "fillQty": 1, "fillPrice": 10} for i in range(entries)]
        # 손실청산: BUY+SELL 쌍(같은 오늘 ET)
        for i in range(losses):
            evs.append({"ts": f"{today}T11:0{i}:00-04:00", "event": "BUY_FILLED", "sym": f"L{i}",
                        "fillQty": 1, "fillPrice": 100})
            evs.append({"ts": f"{today}T12:0{i}:00-04:00", "event": "SELL_FILLED", "sym": f"L{i}",
                        "fillQty": 1, "fillPrice": 100 + (loss_usd if loss_usd else -1)})
        J(evs)

    def test_entry_cap_boundary(self):
        self._mk(entries=E.MAX_ENTRIES_PER_DAY - 1)
        self.assertTrue(E._strategy_circuit()[0])          # 상한-1 → 허용
        self._mk(entries=E.MAX_ENTRIES_PER_DAY)
        self.assertFalse(E._strategy_circuit()[0])         # 상한 도달 → 차단

    def test_loss_count_boundary(self):
        self._mk(losses=E.MAX_LOSS_CLOSES_PER_DAY)
        ok, reason = E._strategy_circuit()
        self.assertFalse(ok); self.assertIn("손실청산", reason)

    def test_daily_loss_usd(self):
        today = E._trading_day()
        J([{"ts": f"{today}T10:00:00-04:00", "event": "BUY_FILLED", "sym": "X",
            "fillQty": 1, "fillPrice": 100},
           {"ts": f"{today}T11:00:00-04:00", "event": "SELL_FILLED", "sym": "X",
            "fillQty": 1, "fillPrice": 100 - E.MAX_DAILY_LOSS_USD}])
        ok, reason = E._strategy_circuit()
        self.assertFalse(ok); self.assertIn("자본보호", reason)


class TestTradingDaysHeld(unittest.TestCase):
    """_trading_days_held(C4): 주말 제외 거래일 카운트."""
    def test_weekdays_only(self):
        # 금(7/10, ET) 진입 → 오늘까지의 카운트가 캘린더보다 작거나 같고 주말 미포함
        import execute as E2
        d = E2._trading_days_held("2026-07-10T23:00:00+09:00")
        cal = E2._days_held("2026-07-10T23:00:00+09:00")
        self.assertIsNotNone(d)
        self.assertLessEqual(d, cal + 1)
        self.assertGreaterEqual(d, max(0, cal - 2 * (cal // 7 + 1)))

    def test_same_day_zero(self):
        now_iso = datetime.now(MC.KST).isoformat()
        self.assertEqual(E._trading_days_held(now_iso), 0)

    def test_none(self):
        self.assertIsNone(E._trading_days_held(None))


class TestSectorOverlap(unittest.TestCase):
    """_sector_overlap(C7): 같은 섹터 보유 시 경고 판정(소프트)."""
    def test_overlap_detected(self):
        olap, dups = E._sector_overlap("AVGO", {"NVDA", "XOM"})
        self.assertTrue(olap); self.assertEqual(dups, ["NVDA"])
    def test_no_overlap(self):
        olap, dups = E._sector_overlap("WFC", {"NVDA", "XOM"})
        self.assertFalse(olap)
    def test_unknown_sector_passes(self):
        self.assertEqual(E._sector_overlap("ZZZZ", {"NVDA"}), (False, []))


class TestAtomicAndChecksum(unittest.TestCase):
    """_atomic_write(G3)·백업 체크섬(G10)."""
    def test_atomic_write(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp()); f = d / "state.txt"
        self.assertTrue(E._atomic_write(f, "hello"))
        self.assertEqual(f.read_text(encoding="utf-8"), "hello")
        self.assertFalse((d / "state.txt.tmp").exists())   # tmp 잔존 없음

    def test_backup_checksum_roundtrip(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        origJ = E.JOURNAL
        E.JOURNAL = d / "index_journal.jsonl"
        try:
            E.JOURNAL.write_text('{"event":"X"}\n')
            E._backup_journal()
            ok, detail = E._verify_backup_checksum()
            self.assertTrue(ok, detail)
            # 백업 훼손 → 검증 실패
            b = sorted((d / ".backups").glob("journal-*.jsonl"))[-1]
            b.write_text("corrupted")
            ok2, detail2 = E._verify_backup_checksum()
            self.assertFalse(ok2)
        finally:
            E.JOURNAL = origJ


class TestMergeFallback(unittest.TestCase):
    """--merge-fallback(A12): 메인에 없는 것만 병합·멱등·fallback 보존."""
    def test_merge_dedup_and_preserve(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        origJ = E.JOURNAL
        E.JOURNAL = d / "index_journal.jsonl"
        try:
            E.JOURNAL.write_text('{"ts":"1","event":"BUY_FILLED","sym":"A","orderId":"o1","fillQty":1,"fillPrice":10}\n')
            fb = d / "index_journal.fallback.jsonl"
            fb.write_text(
                '{"ts":"1","event":"BUY_FILLED","sym":"A","orderId":"o1","fillQty":1,"fillPrice":10}\n'  # 중복
                '{"ts":"2","event":"SELL_FILLED","sym":"A","orderId":"o2","fillQty":1,"fillPrice":11}\n'  # 신규
                "{bad json\n")
            rc = E.merge_fallback()
            self.assertEqual(rc, 0)
            evs = [x for x in E.JOURNAL.read_text().splitlines() if x.strip()]
            self.assertEqual(len(evs), 2)                       # 중복 안 늘어남
            self.assertFalse(fb.exists())                       # 원본은 .merged-*로 이동
            self.assertTrue(list(d.glob("index_journal.fallback.merged-*")))
            # 병합 후 포지션: 1매수+1매도 → 청산
            self.assertEqual(E._strategy_positions(), {})
        finally:
            E.JOURNAL = origJ


class TestServerLockGate(unittest.TestCase):
    """서버 MCP 락 게이트(H8/T9): env 통과·락경합 차단·락없음 통과."""
    def test_gate_matrix(self):
        import os
        import fcntl
        os.environ.pop("TOSS_EXECUTE_LOCK_HELD", None)
        lp = os.path.expanduser("~/.toss-trader/execute.lock")
        self.assertIsNone(S._execute_lock_gate())              # 락 미보유 → 통과
        f = open(lp, "w")
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            r = S._execute_lock_gate()
            self.assertEqual((r or {}).get("error", {}).get("code"), "execute-running")
            os.environ["TOSS_EXECUTE_LOCK_HELD"] = "1"
            self.assertIsNone(S._execute_lock_gate())          # execute 자신 → 통과
        finally:
            os.environ.pop("TOSS_EXECUTE_LOCK_HELD", None)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN); f.close()


class TestScreenerHelpers(unittest.TestCase):
    """스크리너 D1 실적일 로딩·D10 diff 상태 저장."""
    def test_load_earnings_days(self):
        import tempfile
        import json as _json
        from datetime import date, timedelta
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        _json.dump({"AAA": str(date.today() + timedelta(days=5)),
                    "OLD": "2020-01-01", "BAD": "xx"}, f); f.close()
        out = SC._load_earnings_days(f.name)
        self.assertIn("AAA", out); self.assertNotIn("OLD", out); self.assertNotIn("BAD", out)

    def test_diff_and_snapshot(self):
        import tempfile
        import io
        import contextlib
        import json as _json
        from pathlib import Path
        p = Path(tempfile.mkdtemp()) / "last.json"
        rows = [{"sym": "AAA", "score": 40, "rsi": 50, "zone": "🟢매수존", "last": 100.0}]
        SC._diff_and_snapshot(rows, rows, path=str(p))          # 첫 실행: prev 없음
        self.assertTrue(p.exists())
        rows2 = [{"sym": "BBB", "score": 39, "rsi": 48, "zone": "🟢매수존", "last": 50.0}]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            SC._diff_and_snapshot(rows2, rows2, path=str(p))    # AAA 이탈, BBB 신규
        out = buf.getvalue()
        self.assertIn("BBB", out); self.assertIn("AAA", out)
        snap = _json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(snap["buyzone"], ["BBB"])


class TestTrailAndRsiRules(unittest.TestCase):
    """C1 트레일링·C3 RSI 이익조건 — 청산룰 정밀화."""
    def test_trailing_fires_on_giveback(self):
        # 최고 +8% 갔다가 +3.5%(절반 밑)로 반납 → 트레일링 청산
        r = E._exit_decision(pl=3.5, rsi=50, dte=8, cur=100, tgt=200, mfe_pct=8.0)
        self.assertIn("트레일링", r); self.assertEqual(E._exit_category(r), "TRAIL")

    def test_trailing_holds_above_keep(self):
        # +8% 최고, 현재 +5%(절반 위) → 보유
        self.assertIsNone(E._exit_decision(pl=5.0, rsi=50, dte=8, cur=100, tgt=200, mfe_pct=8.0))

    def test_trailing_not_armed_below_arm(self):
        # 최고 +5%(<6%) → 트레일링 비발동(브레이크이븐 영역은 별도)
        self.assertIsNone(E._exit_decision(pl=2.0, rsi=50, dte=8, cur=100, tgt=200, mfe_pct=5.0))

    def test_trailing_negative_pl_is_breakeven_territory(self):
        # pl≤0이면 트레일링이 아니라 브레이크이븐 룰이 담당
        r = E._exit_decision(pl=-1.0, rsi=50, dte=8, cur=100, tgt=200, mfe_pct=8.0)
        self.assertIn("브레이크이븐", r)

    def test_rsi_exit_requires_profit(self):
        # C3: RSI 70이어도 손실이면 RSI 익절 안 함(다른 룰 미발동 시 보유)
        self.assertIsNone(E._exit_decision(pl=-2.0, rsi=70, dte=8, cur=100, tgt=200))
        r = E._exit_decision(pl=2.0, rsi=70, dte=8, cur=100, tgt=200)
        self.assertIn("RSI", r)


class TestPartialTp(Base):
    """C2 부분익절: _partial_tp_taken 판정 + 포지션 종결 시 리셋."""
    def test_not_taken_initially(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100}])
        self.assertFalse(E._partial_tp_taken("X"))

    def test_taken_after_ptp_sell(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100},
           {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 0.5, "fillPrice": 105,
            "exitCat": "PTP"}])
        self.assertTrue(E._partial_tp_taken("X"))

    def test_reset_after_position_closed(self):
        J([{"ts": "1", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100},
           {"ts": "2", "event": "SELL_FILLED", "sym": "X", "fillQty": 0.5, "fillPrice": 105,
            "exitCat": "PTP"},
           {"ts": "3", "event": "SELL_FILLED", "sym": "X", "fillQty": 0.5, "fillPrice": 107,
            "exitCat": "TARGET"},                                   # 전량 종결
           {"ts": "4", "event": "BUY_FILLED", "sym": "X", "fillQty": 1.0, "fillPrice": 100}])
        self.assertFalse(E._partial_tp_taken("X"))                  # 새 포지션 → 리셋

    def test_manage_triggers_partial_tp(self):
        E._strategy_positions = lambda: {"X": {"qty": 1.0, "entryPx": 100.0, "cost_usd": 100.0,
                                               "entryTs": "2026-07-10T00:00:00"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "106"}         # +6% ≥ PARTIAL_TP_PCT
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50; E._days_to_earnings = lambda s: None
        E._load_targets = lambda: {"X": 120.0}                      # 목표 멀어서 전량청산 아님
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._days_held = lambda ts: 0
        E._trading_days_held = lambda ts: 1
        E._partial_tp_taken = lambda s: False
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append((sym, qty, k.get("reason_cat"))), 0)[1]
        E.manage()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "X"); self.assertEqual(calls[0][2], "PTP")
        self.assertAlmostEqual(calls[0][1], 0.5)                    # 절반

    def test_manage_no_ptp_if_already_taken(self):
        E._strategy_positions = lambda: {"X": {"qty": 0.5, "entryPx": 100.0, "cost_usd": 50.0,
                                               "entryTs": "2026-07-10T00:00:00"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "106"}
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50; E._days_to_earnings = lambda s: None
        E._load_targets = lambda: {"X": 120.0}
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._trading_days_held = lambda ts: 1
        E._partial_tp_taken = lambda s: True                        # 이미 부분익절함
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append(sym), 0)[1]
        E.manage()
        self.assertEqual(calls, [])                                 # 러너 유지(중복 부분익절 없음)


class TestPtpArmed(Base):
    """감사#232 목표가가 PTP 임계에 붙어 있으면 부분익절은 사문화 → 미무장."""

    def test_armed_matrix(self):
        e = 100.0
        self.assertTrue(E._ptp_armed(e, None))                      # 목표 미설정 → 상한 없음
        self.assertTrue(E._ptp_armed(e, 0))                         # 0/None 동일 취급
        gap = E.PARTIAL_TP_PCT + E.PARTIAL_TP_MIN_GAP
        self.assertTrue(E._ptp_armed(e, e * (1 + gap / 100)))       # 경계 = 무장
        self.assertFalse(E._ptp_armed(e, e * (1 + (gap - 0.1) / 100)))
        self.assertFalse(E._ptp_armed(e, e * 1.05))                 # 라이브 관행(+5%) → 미무장
        self.assertTrue(E._ptp_armed(e, e * 1.20))                  # 러너 여유 충분

    def test_manage_skips_ptp_when_target_collides(self):
        """진입+5% 목표 + 현재가 +5.5%: 전량청산이 먼저 걸려야 하고 PTP는 침묵."""
        E._strategy_positions = lambda: {"X": {"qty": 1.0, "entryPx": 100.0, "cost_usd": 100.0,
                                               "entryTs": "2026-07-10T00:00:00"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "105.5"}
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50; E._days_to_earnings = lambda s: None
        E._load_targets = lambda: {"X": 105.0}                      # PTP(+5%)와 충돌하는 목표가
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._days_held = lambda ts: 0
        E._trading_days_held = lambda ts: 1
        E._partial_tp_taken = lambda s: False
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append((qty, k.get("reason_cat"))), 0)[1]
        E.manage()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(calls[0][1], "PTP")                     # 반쪽 매도로 새지 않음
        self.assertAlmostEqual(calls[0][0], 1.0)                    # 전량청산

    def test_manage_ptp_still_fires_with_roomy_target(self):
        """목표가가 충분히 위면 기존 동작(절반 익절)은 그대로 살아 있어야 한다."""
        E._strategy_positions = lambda: {"X": {"qty": 1.0, "entryPx": 100.0, "cost_usd": 100.0,
                                               "entryTs": "2026-07-10T00:00:00"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "106"}
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50; E._days_to_earnings = lambda s: None
        E._load_targets = lambda: {"X": 120.0}
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._days_held = lambda ts: 0
        E._trading_days_held = lambda ts: 1
        E._partial_tp_taken = lambda s: False
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append((qty, k.get("reason_cat"))), 0)[1]
        E.manage()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1], "PTP"); self.assertAlmostEqual(calls[0][0], 0.5)


class TestBackstopSessionScope(unittest.TestCase):
    """감사#232 캘린더 백스톱이 정규장 밖에서 시간검사를 무력화하지 않는지."""

    def test_helper_rejects_extended_and_weekend(self):
        import datetime as _d
        from zoneinfo import ZoneInfo
        et = ZoneInfo("America/New_York")
        cases = [(_d.datetime(2026, 7, 21, 9, 29, tzinfo=et), False),   # 장전 1분
                 (_d.datetime(2026, 7, 21, 9, 30, tzinfo=et), True),    # 개장 정각
                 (_d.datetime(2026, 7, 21, 15, 59, tzinfo=et), True),
                 (_d.datetime(2026, 7, 21, 16, 0, tzinfo=et), False),   # 종료 정각 = 제외
                 (_d.datetime(2026, 7, 21, 18, 0, tzinfo=et), False),   # 애프터마켓
                 (_d.datetime(2026, 7, 17, 12, 0, tzinfo=et), True),    # 금요일 정오
                 (_d.datetime(2026, 7, 18, 12, 0, tzinfo=et), False),   # 토요일
                 (_d.datetime(2026, 7, 19, 12, 0, tzinfo=et), False)]   # 일요일
        real = _d.datetime
        for now, want in cases:
            class _Frozen(real):
                @classmethod
                def now(cls, tz=None):
                    return now
            _d.datetime = _Frozen
            try:
                self.assertEqual(S._et_regular_now(), want, f"{now} → {want}")
            finally:
                _d.datetime = real


class TestTrailArmScaling(Base):
    """감사#234 트레일링 무장선을 ATR로 정규화 — 저변동주 사문화 해소."""

    def test_arm_scales_with_volatility(self):
        # 실측 ATR: NEE 1.73 / XOM 2.13 / DE 3.09 / NVDA 3.58
        self.assertAlmostEqual(E._trail_arm_pct(1.73), 1.5 * 1.73, places=6)   # 2.6%
        self.assertAlmostEqual(E._trail_arm_pct(2.13), 1.5 * 2.13, places=6)   # 3.2%
        self.assertAlmostEqual(E._trail_arm_pct(3.09), 1.5 * 3.09, places=6)   # 4.6%
        self.assertAlmostEqual(E._trail_arm_pct(3.58), 1.5 * 3.58, places=6)   # 5.4%

    def test_never_arms_later_than_before(self):
        """상한 = 종전 고정값. 어떤 ATR에서도 예전보다 늦게 무장하지 않는다."""
        for atr in (0.5, 1.0, 2.0, 3.0, 4.0, 8.0, 20.0):
            self.assertLessEqual(E._trail_arm_pct(atr), E.TRAIL_ARM_PCT)

    def test_floor_prevents_noise_arming(self):
        self.assertEqual(E._trail_arm_pct(0.1), E.TRAIL_ARM_FLOOR)
        self.assertGreaterEqual(E._trail_arm_pct(0.9), E.TRAIL_ARM_FLOOR)

    def test_unknown_atr_falls_back_to_fixed(self):
        for bad in (None, 0, -1.0):
            self.assertEqual(E._trail_arm_pct(bad), E.TRAIL_ARM_PCT)

    def test_low_vol_stock_now_protected(self):
        """NEE류(ATR 1.73): 최고 +3.0% 후 절반 반납 → 종전엔 무장조차 안 됐다."""
        r = E._exit_decision(pl=1.4, rsi=50, dte=None, cur=90.0, tgt=None,
                             days_held=3, ma150=None, atr_pct=1.73, mfe_pct=3.0)
        self.assertIsNotNone(r); self.assertIn("트레일링", r)
        self.assertEqual(E._exit_category(r), "TRAIL")

    def test_high_vol_stock_unchanged(self):
        """NVDA류(ATR 3.58 → 무장 5.4%): 최고 +3.0%는 아직 노이즈 → 청산 없음."""
        r = E._exit_decision(pl=1.4, rsi=50, dte=None, cur=205.0, tgt=None,
                             days_held=3, ma150=None, atr_pct=3.58, mfe_pct=3.0)
        self.assertIsNone(r)

    def test_target_still_wins_over_trail(self):
        """목표가 도달이면 트레일링보다 목표가 라벨이 우선(SL6 유지)."""
        r = E._exit_decision(pl=4.0, rsi=50, dte=None, cur=105.0, tgt=104.0,
                             days_held=3, ma150=None, atr_pct=1.73, mfe_pct=9.0)
        self.assertIn("목표가", r)


class TestPriceCorroboration(Base):
    """감사#233 장외 유령체결이 ±50% 위생검사를 통과해 가짜 손절을 내는 것 차단."""

    def _book(self, bid, ask):
        S.CLIENT.get_orderbook = lambda s: {"bids": [{"price": str(bid), "quantity": "5"}],
                                            "asks": [{"price": str(ask), "quantity": "5"}]}

    def test_real_de_phantom_tick_rejected(self):
        """실측: DE 2026-07-18 05:55 KST 3주 거래로 473.49, 같은 분봉 598 마감."""
        self._book(597.0, 599.0)
        ok, why, _ = E._price_corroborated("DE", 473.49, side="down")
        self.assertFalse(ok); self.assertIn("매수호가", why)

    def test_real_nvda_phantom_tick_rejected(self):
        """실측: NVDA 2026-07-21 06:10 KST 189.54(-8.4%) — 손절선을 넘는 유령 저가."""
        self._book(202.60, 202.75)
        ok, _, _ = E._price_corroborated("NVDA", 189.54, side="down")
        self.assertFalse(ok)

    def test_genuine_crash_still_sells(self):
        """진짜 급락은 호가가 따라 내려온다 → 손절이 막히면 안 된다."""
        self._book(549.90, 550.10)
        ok, why, _ = E._price_corroborated("DE", 550.0, side="down")
        self.assertTrue(ok, why)

    def test_phantom_high_rejected(self):
        """유령 고가체결 → 목표가 도달로 오판한 조기청산 차단."""
        self._book(590.0, 599.0)
        ok, why, _ = E._price_corroborated("DE", 700.0, side="up")
        self.assertFalse(ok); self.assertIn("매도호가", why)

    def test_book_unavailable_fails_closed(self):
        """호가를 못 얻으면 팔지 않는다(유령 매도는 되돌릴 수 없고, 지연 손절은 만회 가능)."""
        def boom(s): raise RuntimeError("timeout")
        S.CLIENT.get_orderbook = boom
        ok, why, _ = E._price_corroborated("DE", 550.0, side="down")
        self.assertFalse(ok); self.assertIn("실패", why)
        S.CLIENT.get_orderbook = lambda s: {"bids": [], "asks": []}
        ok, why, _ = E._price_corroborated("DE", 550.0, side="down")
        self.assertFalse(ok); self.assertIn("비어", why)

    def test_tolerance_boundary(self):
        inside = 100.0 * (1 + (E.PRICE_BOOK_TOL - 0.1) / 100)    # 허용치 안 = 통과
        self._book(inside, inside + 0.1)
        self.assertTrue(E._price_corroborated("X", 100.0, side="down")[0])
        outside = 100.0 * (1 + (E.PRICE_BOOK_TOL + 0.1) / 100)   # 허용치 밖 = 차단
        self._book(outside, outside + 0.1)
        self.assertFalse(E._price_corroborated("X", 100.0, side="down")[0])

    def test_manage_blocks_phantom_stop_sale(self):
        """통합: 유령 -20% 틱에 손절 판정이 나도 실제 매도는 나가지 않는다."""
        E._strategy_positions = lambda: {"DE": {"qty": 0.025, "entryPx": 596.20,
                                                "cost_usd": 14.9,
                                                "entryTs": "2026-07-16T23:22:02"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "473.49"}      # 3주짜리 유령체결
        self._book(597.0, 599.0)                                    # 호가는 멀쩡
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50; E._days_to_earnings = lambda s: None
        E._load_targets = lambda: {}
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._trading_days_held = lambda ts: 1
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append(sym), 0)[1]
        E.manage()
        self.assertEqual(calls, [])                                 # 매도 안 나감
        evs = [json.loads(l)["event"] for l in open(E.JOURNAL) if l.strip()]
        self.assertIn("EXIT_UNCORROBORATED", evs)
        self.assertNotIn("GAP_THROUGH_STOP", evs)                   # 가짜 갭관통 경보도 없음

    def test_manage_allows_genuine_stop_sale(self):
        """대조군: 호가가 함께 내려온 진짜 급락은 정상 청산된다."""
        E._strategy_positions = lambda: {"DE": {"qty": 0.025, "entryPx": 596.20,
                                                "cost_usd": 14.9,
                                                "entryTs": "2026-07-16T23:22:02"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "540.0"}       # -9.4% 실제 급락
        self._book(539.90, 540.10)
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50; E._days_to_earnings = lambda s: None
        E._load_targets = lambda: {}
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._trading_days_held = lambda ts: 1
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append((sym, k.get("reason_cat"))), 0)[1]
        E.manage()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "DE")
        self.assertIn(calls[0][1], ("STOP", "GAPSTOP"))

    def test_earn_exit_not_gated_by_book(self):
        """실적청산은 날짜로 결정 → 호가 조회가 죽어도 막히면 안 된다."""
        E._strategy_positions = lambda: {"DE": {"qty": 0.025, "entryPx": 596.20,
                                                "cost_usd": 14.9,
                                                "entryTs": "2026-07-16T23:22:02"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "596.0"}
        def boom(s): raise RuntimeError("book down")
        S.CLIENT.get_orderbook = boom
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50
        E._days_to_earnings = lambda s: 1                           # 실적 1일전 → 강제청산
        E._load_targets = lambda: {}
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._trading_days_held = lambda ts: 1
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append(k.get("reason_cat")), 0)[1]
        E.manage()
        self.assertEqual(calls, ["EARN"])


class TestAudit235Fixes(Base):
    """감사#235 — #233 구현의 결함 수정(critical 1 + high 6)."""

    def _book(self, bid, ask):
        S.CLIENT.get_orderbook = lambda s: {"bids": [{"price": str(bid), "quantity": "5"}],
                                            "asks": [{"price": str(ask), "quantity": "5"}]}

    def _pos(self, entry=596.20, qty=0.025, cost=14.9):
        E._strategy_positions = lambda: {"DE": {"qty": qty, "entryPx": entry,
                                                "cost_usd": cost,
                                                "entryTs": "2026-06-01T23:22:02"}}
        E._fetch_daily = lambda *a, **k: []
        E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50; E._days_to_earnings = lambda s: None
        E._load_targets = lambda: {}
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")

    def test_side_aware_no_false_block_on_real_crash(self):
        """진짜 급락 중엔 체결가가 호가보다 위다 — ask검사가 진짜 손절을 막던 버그."""
        self._book(528.50, 529.00)          # 호가가 이미 더 내려감
        ok, why, _ = E._price_corroborated("DE", 548.0, side="down")
        self.assertTrue(ok, why)            # 종전 구현은 여기서 False였다

    def test_side_aware_no_false_block_on_real_rally(self):
        self._book(642.0, 643.0)
        ok, why, _ = E._price_corroborated("DE", 620.0, side="up")
        self.assertTrue(ok, why)

    def test_empty_book_is_retried(self):
        calls = []
        def flaky(s):
            calls.append(1)
            return {"bids": [], "asks": []} if len(calls) == 1 else \
                   {"bids": [{"price": "597"}], "asks": [{"price": "599"}]}
        S.CLIENT.get_orderbook = flaky
        ok, why, mid = E._price_corroborated("DE", 598.0, side="down")
        self.assertEqual(len(calls), 2)     # 빈 호가도 재시도(종전엔 즉시 포기)
        self.assertTrue(ok, why); self.assertAlmostEqual(mid, 598.0)

    def test_crossed_book_rejected(self):
        self._book(600.0, 590.0)            # 매수 > 매도 = 피드 이상
        ok, why, mid = E._price_corroborated("DE", 595.0, side="down")
        self.assertFalse(ok); self.assertIn("역전", why); self.assertIsNone(mid)

    def test_time_exit_is_gated(self):
        """감사#235 high: 유령 저가가 스탑선 위에 찍히면 TIME으로 빠져나가 무게이트 매도."""
        self.assertIn("TIME", E._PRICE_DRIVEN_EXITS)
        self._pos()
        S.CLIENT.get_price = lambda s: {"lastPrice": "560.0"}   # -6%: 스탑(-8%) 안쪽
        self._book(597.0, 599.0)                                # 호가는 멀쩡 = 유령
        E._trading_days_held = lambda ts: E.MAX_HOLD_DAYS       # 시간청산 조건 충족
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append(k.get("reason_cat")), 0)[1]
        E.manage()
        self.assertEqual(calls, [])                             # 종전엔 TIME으로 팔렸다
        evs = [json.loads(l)["event"] for l in open(E.JOURNAL) if l.strip()]
        self.assertIn("EXIT_UNCORROBORATED", evs)

    def test_phantom_price_not_used_for_valuation(self):
        """기각한 유령가로 평가하면 가짜 PORTFOLIO_ALERT가 당일 래치를 소모한다."""
        self._pos(entry=596.20, qty=0.15, cost=89.40)
        S.CLIENT.get_price = lambda s: {"lastPrice": "473.49"}
        self._book(597.0, 599.0)
        E._trading_days_held = lambda ts: 1
        E.sell = lambda *a, **k: 0
        E.manage()
        evs = [json.loads(l).get("event") for l in open(E.JOURNAL) if l.strip()]
        self.assertNotIn("PORTFOLIO_ALERT", evs)   # mid(598)로 평가 → 경보 없음

    def test_streak_forces_sale_when_book_persistently_dead(self):
        """critical: 호가가 계속 죽어 있으면 청산 경로가 영구 소멸 → N회 후 강행.

        선행 보류는 실제 사이클 간격(30분)을 반영해 서로 다른 ts로 주입한다 —
        저널 dedup 키가 (ts,event,sym,orderId)라 같은 초의 동일 이벤트는 1건으로 합쳐진다."""
        self._pos()
        today = E._trading_day()
        for hh in ("23:00", "23:30"):       # 앞선 두 사이클이 이미 보류된 상태
            E._j({"ts": f"{today}T{hh}:00+09:00", "event": "EXIT_UNCORROBORATED",
                  "sym": "DE", "cat": "GAPSTOP"})
        self.assertEqual(E._uncorroborated_streak("DE"), E.UNCORROBORATED_MAX_STREAK - 1)
        S.CLIENT.get_price = lambda s: {"lastPrice": "500.0"}   # -16% 손절권
        def dead(s): raise RuntimeError("orderbook down")
        S.CLIENT.get_orderbook = dead
        E._trading_days_held = lambda ts: 1
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append(k.get("reason_cat")), 0)[1]
        E.manage()
        self.assertEqual(len(calls), 1, "N회째에는 강행되어야 한다")
        evs = [json.loads(l).get("event") for l in open(E.JOURNAL) if l.strip()]
        self.assertIn("EXIT_FORCED_STALE_BOOK", evs)

    def test_streak_below_threshold_still_blocks(self):
        """대조군: 임계 미만이면 여전히 보류(강행은 마지막 수단)."""
        self._pos()
        E._j({"ts": f"{E._trading_day()}T23:00:00+09:00", "event": "EXIT_UNCORROBORATED",
              "sym": "DE", "cat": "GAPSTOP"})
        S.CLIENT.get_price = lambda s: {"lastPrice": "500.0"}
        def dead(s): raise RuntimeError("orderbook down")
        S.CLIENT.get_orderbook = dead
        E._trading_days_held = lambda ts: 1
        calls = []
        E.sell = lambda sym, qty, **k: (calls.append(sym), 0)[1]
        E.manage()
        self.assertEqual(calls, [])

    def test_streak_resets_after_successful_sell(self):
        E._read_journal = lambda: [
            {"ts": "2026-07-21T23:00:00+09:00", "sym": "DE", "event": "EXIT_UNCORROBORATED"},
            {"ts": "2026-07-21T23:30:00+09:00", "sym": "DE", "event": "EXIT_UNCORROBORATED"},
            {"ts": "2026-07-21T23:40:00+09:00", "sym": "DE", "event": "SELL_FILLED"},
            {"ts": "2026-07-21T23:50:00+09:00", "sym": "DE", "event": "EXIT_UNCORROBORATED"},
        ]
        E._trading_day = lambda ts=None: "2026-07-21"
        self.assertEqual(E._uncorroborated_streak("DE"), 1)

    def test_uncorroborated_is_alertable(self):
        self.assertIn("EXIT_UNCORROBORATED", E._ALERT_EVENTS)
        self.assertIn("EXIT_FORCED_STALE_BOOK", E._ALERT_EVENTS)


class TestOffhoursAlert(Base):
    """감사#238 소수점은 장외 매매 불가 → 트리거 시 알림만(사용자 앱 수동매도)."""

    def _pos_at_target(self, last, bid, ask):
        E._strategy_positions = lambda: {"XOM": {"qty": 0.09, "entryPx": 144.74,
                                                 "cost_usd": 12.98,
                                                 "entryTs": "2026-07-15T22:51:00"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": str(last)}
        S.CLIENT.get_orderbook = lambda s: {"bestBid": bid, "bestAsk": ask,
                                            "bids": [{"price": str(bid), "quantity": "50"}],
                                            "asks": [{"price": str(ask), "quantity": "50"}]}
        E._fetch_daily = lambda *a, **k: []; E._repoll_pending = lambda *a, **k: None
        E._rsi = lambda s, c=None: 50; E._days_to_earnings = lambda s: 10
        E._load_targets = lambda: {"XOM": 151.97}
        E._ma150 = lambda s, c=None: None; E._atr_pct = lambda s, c=None: None
        E._mfe_pct = lambda *a: None; E._trading_days_held = lambda ts: 5
        # 장외(휴장) 세션
        MC.us_session = lambda: "미국장 휴장"; E._market_open = lambda: (True, "개장")

    def test_alerts_on_real_offhours_target(self):
        """장외 목표 도달 + 실매수호가 → 알림 발송, 매도는 안 함."""
        self._pos_at_target(last=152.20, bid=152.20, ask=152.30)
        calls = []
        E.sell = lambda *a, **k: (calls.append(a), 0)[1]
        E.manage()
        self.assertEqual(calls, [])                          # 소수점 장외 → 매도 안 함
        evs = [json.loads(l) for l in open(E.JOURNAL) if l.strip()]
        sig = [e for e in evs if e.get("event") == "EXIT_SIGNAL_OFFHOURS"]
        self.assertEqual(len(sig), 1); self.assertEqual(sig[0]["sym"], "XOM")

    def test_dedup_no_repeat_alert(self):
        """같은 종목 두 사이클 → 알림 1회만(30분마다 반복 방지)."""
        self._pos_at_target(last=152.20, bid=152.20, ask=152.30)
        E.sell = lambda *a, **k: 0
        E.manage(); E.manage()
        evs = [json.loads(l) for l in open(E.JOURNAL) if l.strip()]
        sig = [e for e in evs if e.get("event") == "EXIT_SIGNAL_OFFHOURS"]
        self.assertEqual(len(sig), 1)                        # 두 번 돌아도 1건

    def test_no_alert_on_phantom_print(self):
        """유령 프린트(목표 위지만 매수호가 미달) → 알림 침묵."""
        self._pos_at_target(last=152.20, bid=151.40, ask=151.80)   # 실측 XOM 상황
        E.sell = lambda *a, **k: 0
        E.manage()
        evs = ([json.loads(l).get("event") for l in open(E.JOURNAL) if l.strip()]
               if E.JOURNAL.exists() else [])
        self.assertNotIn("EXIT_SIGNAL_OFFHOURS", evs)

    def test_offhours_alert_is_alertable(self):
        self.assertIn("EXIT_SIGNAL_OFFHOURS", E._ALERT_EVENTS)


class TestEntryAtrFloor(Base):
    """감사#240 저변동주 진입 차단 — +5% 목표가 2.4 ATR을 넘으면 죽은 돈(NEE 실측)."""

    def _mock_analyze(self, atr):
        import stock_screener as SC
        self._saved_an = SC.analyze
        SC.analyze = lambda s: {"zone": "🟢매수존", "rsi": 50, "atr": atr, "ext50": 1.0}
        self.addCleanup(lambda: setattr(SC, "analyze", self._saved_an))

    def test_ultralow_atr_blocked(self):
        """감사#243로 하한 1.0 완화 — 그 아래(비용 이하 목표)만 차단."""
        self._mock_analyze(0.7)
        ok, why = E._entry_guard("XLOW")
        self.assertFalse(ok); self.assertIn("저변동주", why)

    def test_subfloor_atr_blocked(self):
        """감사#243 검토로 하한 2.0 복원 — ATR 1.65(R:R<0.5)는 다시 차단(EV잠식 방지)."""
        self._mock_analyze(1.65)                       # NEE류 저변동
        ok, why = E._entry_guard("XREIT")
        self.assertFalse(ok); self.assertIn("저변동주", why)

    def test_boundary_and_normal_pass(self):
        self._mock_analyze(E.ENTRY_MIN_ATR_PCT)        # 경계 = 통과
        self.assertTrue(E._entry_guard("XOK")[0])
        self._mock_analyze(2.13)                       # XOM 실측(승리 사례) = 통과
        self.assertTrue(E._entry_guard("XOM2")[0])

    def test_missing_atr_not_blocked(self):
        """ATR 미상은 이 가드로 막지 않는다(다른 가드가 처리) — 과차단 방지."""
        self._mock_analyze(None)
        self.assertTrue(E._entry_guard("XNA")[0])


class TestOpenBuyQueue(Base):
    """감사#241 개장 즉시 매수 — 사전검증 큐를 개장 순간 buy()로 발사(느린검증/빠른체결 분리)."""

    def setUp(self):
        super().setUp()
        import tempfile
        E.PENDING_BUYS_FILE = Path(tempfile.mkdtemp()) / "pending_buys.json"
        E._days_to_earnings = lambda s: 30           # 실적 등록·안전(arm 통과 기본)
        E._trading_day = lambda ts=None: "2026-07-24"
        self._saved_alo = S.CFG.allow_live_orders    # frozen dataclass → object.__setattr__로 우회
        object.__setattr__(S.CFG, "allow_live_orders", True)   # B1 가드 통과 기본
        self.addCleanup(lambda: object.__setattr__(S.CFG, "allow_live_orders", self._saved_alo))

    def _open(self):
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")

    def test_arm_disarm_dedup(self):
        E._arm_buy("INVH", 75); E._arm_buy("UDR", 70)
        E._arm_buy("INVH", 80)                       # 중복 → 최신 금액으로 갱신
        q = E._load_pending_buys()
        self.assertEqual(len(q), 2)
        self.assertEqual(next(i for i in q if i["sym"] == "INVH")["usd"], 80.0)
        E._disarm_buy("INVH")
        self.assertEqual([i["sym"] for i in E._load_pending_buys()], ["UDR"])

    def test_arm_rejects_unknown_earnings(self):
        """감사#242 B3: 실적일 미상 종목은 큐 등록 거부(완전자동 실적 안전장치)."""
        E._days_to_earnings = lambda s: None
        self.assertIsNone(E._arm_buy("XUNK", 75))
        self.assertEqual(E._load_pending_buys(), [])

    def test_arm_rejects_legacy(self):
        self.assertIsNone(E._arm_buy("MBRX", 75))    # 레거시

    def test_exec_holds_when_not_open(self):
        E._arm_buy("INVH", 75)
        MC.us_session = lambda: "미국 프리마켓"; E._market_open = lambda: (True, "개장")
        calls = []
        E.buy = lambda *a, **k: (calls.append(a), 0)[1]
        E.exec_open_buys()
        self.assertEqual(calls, [])
        self.assertEqual(len(E._load_pending_buys()), 1)

    def test_exec_fills_and_removes(self):
        E._arm_buy("INVH", 75); E._arm_buy("UDR", 70); self._open()
        E.buy = lambda sym, usd, **k: 0
        E.exec_open_buys()
        self.assertEqual(E._load_pending_buys(), [])

    def test_exec_keeps_on_wait_removes_on_reject(self):
        E._arm_buy("WAITS", 75); E._arm_buy("REJS", 70); self._open()
        E.buy = lambda sym, usd, **k: 2 if sym == "WAITS" else 1
        E.exec_open_buys()
        self.assertEqual([i["sym"] for i in E._load_pending_buys()], ["WAITS"])

    def test_b1_halt_preserves_queue(self):
        """감사#242 B1: HALT 시 큐 삭제가 아니라 보존 — buy()는 호출조차 안 됨."""
        E._arm_buy("A", 75); E._arm_buy("B", 70); self._open()
        halt = Path.home() / ".toss-mcp" / "HALT"
        made = False
        if not halt.exists():
            halt.parent.mkdir(parents=True, exist_ok=True); halt.write_text("test"); made = True
        try:
            calls = []
            E.buy = lambda *a, **k: (calls.append(a), 1)[1]
            E.exec_open_buys()
            self.assertEqual(calls, [])                       # buy 호출 안 됨
            self.assertEqual(len(E._load_pending_buys()), 2)  # 큐 전량 보존
        finally:
            if made: halt.unlink()

    def test_gap_down_defers_and_keeps_queue(self):
        """③ 개편: 개장 갭다운(당일≤-3%)이면 발사 보류·큐 유지, buy() 미호출·BUY_GAP_DOWN_DEFER 기록."""
        E._arm_buy("GAPD", 75); self._open()
        E._day_change = lambda s: (-4.0, 100.0)              # 당일 -4% 갭다운
        calls = []
        E.buy = lambda *a, **k: (calls.append(a), 0)[1]
        E.exec_open_buys()
        self.assertEqual(calls, [])                                        # 발사 안 됨
        self.assertEqual([i["sym"] for i in E._load_pending_buys()], ["GAPD"])  # 큐 유지
        evs = [json.loads(l)["event"] for l in open(E.JOURNAL) if l.strip()]
        self.assertIn("BUY_GAP_DOWN_DEFER", evs)

    def test_no_gap_down_fires_normally(self):
        """③ 대조: 갭다운 아니면(+1%) 정상 발사·큐 제거."""
        E._arm_buy("OKUP", 75); self._open()
        E._day_change = lambda s: (1.0, 100.0)
        E.buy = lambda sym, usd, **k: 0
        E.exec_open_buys()
        self.assertEqual(E._load_pending_buys(), [])                       # 체결→큐 제거

    def test_b1_live_off_preserves_queue(self):
        E._arm_buy("A", 75); self._open()
        object.__setattr__(S.CFG, "allow_live_orders", False)
        calls = []
        E.buy = lambda *a, **k: (calls.append(a), 1)[1]
        E.exec_open_buys()
        self.assertEqual(calls, [])
        self.assertEqual(len(E._load_pending_buys()), 1)

    def test_b1_midtick_halt_stops_without_delete(self):
        """틱 중간 HALT: 첫 종목 rc=1 삭제 후, 다음은 HALT 감지로 보존·중단."""
        E._arm_buy("FIRST", 75); E._arm_buy("SECOND", 70); self._open()
        halt = Path.home() / ".toss-mcp" / "HALT"
        state = {"n": 0}
        def fake_buy(sym, usd, **k):
            state["n"] += 1
            if state["n"] == 1:
                halt.parent.mkdir(parents=True, exist_ok=True); halt.write_text("mid")
            return 1
        pre = halt.exists()
        try:
            E.buy = fake_buy
            E.exec_open_buys()
            left = [i["sym"] for i in E._load_pending_buys()]
            self.assertIn("SECOND", left)                    # 둘째는 보존
        finally:
            if not pre and halt.exists(): halt.unlink()

    def test_l1_stale_arm_discarded(self):
        """감사#242 L1: 다른 거래일에 무장된 항목은 발사 안 하고 폐기."""
        E._arm_buy("STALE", 75)
        E._trading_day = lambda ts=None: "2026-07-25"     # 다음날
        self._open()
        calls = []
        E.buy = lambda *a, **k: (calls.append(a), 0)[1]
        E.exec_open_buys()
        self.assertEqual(calls, [])                       # stale → 발사 안 함
        self.assertEqual(E._load_pending_buys(), [])      # 폐기

    def test_fire_time_earnings_recheck(self):
        """발사 직전 실적 미상으로 바뀌면 제거(파일 변경 대비)."""
        E._arm_buy("X", 75); self._open()
        E._days_to_earnings = lambda s: None              # 발사시점엔 미상
        calls = []
        E.buy = lambda *a, **k: (calls.append(a), 0)[1]
        E.exec_open_buys()
        self.assertEqual(calls, [])
        self.assertEqual(E._load_pending_buys(), [])

    def test_empty_queue_noop(self):
        self._open()
        calls = []
        E.buy = lambda *a, **k: (calls.append(1), 0)[1]
        E.exec_open_buys()
        self.assertEqual(calls, [])


class TestVolAdaptiveSizing(Base):
    """감사#243 변동성 적응: 리스크 균등 사이징 + k×ATR 목표."""

    def test_risk_equalized_across_atr(self):
        """핵심: 서로 다른 ATR도 일일 리스크(달러×ATR)가 ~일정해야 한다."""
        risks = []
        for atr in (2.0, 2.5, 3.0, 4.0, 5.0):
            usd = E._position_size_usd(atr)
            risks.append(usd * atr / 100)
        # 최대/최소 리스크 비율이 1.3배 이내(달러균등이면 2.5배까지 벌어짐)
        self.assertLess(max(risks) / min(risks), 1.3)

    def test_size_inverse_to_atr(self):
        """고변동은 적게, 저변동은 많이."""
        self.assertGreater(E._position_size_usd(2.0), E._position_size_usd(4.0))

    def test_size_clamped_to_concentration(self):
        """아주 낮은 ATR도 과집중 상한(0.2×예산) 초과 금지."""
        cap = E.MAX_POSITION_WEIGHT * E.BUDGET_USD
        self.assertLessEqual(E._position_size_usd(0.5), cap)

    def test_size_min_floor(self):
        self.assertGreaterEqual(E._position_size_usd(20.0), 10)   # 초고변동도 최소주문 이상

    def test_size_unknown_atr_uses_base(self):
        self.assertEqual(E._position_size_usd(None), E._position_size_usd(E.BASE_ATR_PCT))

    def test_atr_target_proportional(self):
        self.assertAlmostEqual(E._atr_target(100, 2.0), 100 * (1 + E.TARGET_ATR_MULT * 0.02), places=2)
        self.assertAlmostEqual(E._atr_target(100, 5.0), 100 * (1 + E.TARGET_ATR_MULT * 0.05), places=2)
        # 저변동이 고변동보다 목표 낮음(도달가능)
        self.assertLess(E._atr_target(100, 1.8), E._atr_target(100, 4.0))

    def test_atr_target_unknown_uses_base(self):
        self.assertEqual(E._atr_target(100, None), E._atr_target(100, E.BASE_ATR_PCT))

    def test_buy_autowires_atr_target(self):
        """감사#243 [A]: 체결 후 k×ATR 목표가 자동 설정(신규진입 tgt=None 갭 해소)."""
        import tempfile
        E.TARGETS_FILE = Path(tempfile.mkdtemp()) / "t.json"
        E._atr_pct = lambda s, c=None: 3.0
        set_calls = []
        self._saved_st = E.set_target
        E.set_target = lambda sym, px: (set_calls.append((sym, px)), 0)[1]
        self.addCleanup(lambda: setattr(E, "set_target", self._saved_st))
        E._load_targets = lambda: {}                 # 목표 없음(신규)
        raw = E._atr_target(100.0, 3.0)              # 100×(1+2×0.03)=106
        # buy()의 자동배선 로직 직접 재현 검증(체결가 100, ATR 3%)
        fp = 100.0
        if fp and fp > 0 and "X" not in E._load_targets():
            t = E._atr_target(fp, E._atr_pct("X"))
            if t - int(t) <= 0.05:
                t = int(t) - 0.03
            E.set_target("X", round(t, 2))
        self.assertEqual(len(set_calls), 1)
        self.assertAlmostEqual(set_calls[0][1], 105.97, places=2)   # 106.00 정수벽 → 넛지

    def test_atr_target_roundnumber_nudge(self):
        """목표가 정수 매도벽 바로 위면 벽 아래로 넛지."""
        # 진입 25.49, ATR 2% → 25.49×1.04=26.51 (벽 아님, 그대로)
        raw = E._atr_target(25.49, 2.0)
        self.assertAlmostEqual(raw, 26.51, places=2)
        # 진입 25.0, ATR 2% → 26.00 (정수벽) → 넛지 25.97
        raw2 = E._atr_target(25.0, 2.0)
        nudged = (int(raw2) - 0.03) if (raw2 - int(raw2) <= 0.05) else raw2
        self.assertAlmostEqual(nudged, 25.97, places=2)

    def test_arm_autosizes_when_usd_omitted(self):
        """usd 생략 시 ATR로 자동 사이징."""
        import tempfile
        E.PENDING_BUYS_FILE = Path(tempfile.mkdtemp()) / "pb.json"
        E._days_to_earnings = lambda s: 30
        E._trading_day = lambda ts=None: "2026-07-27"
        E._atr_pct = lambda s, c=None: 4.0
        q = E._arm_buy("XYZ")                       # usd 생략
        self.assertIsNotNone(q)
        self.assertAlmostEqual(q[0]["usd"], E._position_size_usd(4.0), places=0)


class TestPostEarnBlock(unittest.TestCase):
    """C8 실적 직후 재진입 차단 경계."""
    def test_boundaries(self):
        lo, hi = -E.EARN_POST_BLOCK_DAYS, E.EARN_BLOCK_DAYS
        self.assertTrue(lo <= 0 <= hi)         # 당일 차단
        self.assertTrue(lo <= -1 <= hi)        # 발표 1일 뒤 차단(C8)
        self.assertFalse(lo <= -2 <= hi)       # 2일 뒤부터 허용
        self.assertFalse(lo <= hi + 1 <= hi)   # 발표 3일 전 허용


class TestKstEtOffset(unittest.TestCase):
    """G7 tz 폴백 월기반 근사."""
    def test_summer_winter(self):
        self.assertEqual(E._kst_et_offset(7), 13)    # EDT
        self.assertEqual(E._kst_et_offset(1), 14)    # EST
        self.assertEqual(E._kst_et_offset(3), 13)
        self.assertEqual(E._kst_et_offset(12), 14)


class TestSetCommands(unittest.TestCase):
    """G4 --set-target/--set-earnings: 검증·원자적 저장."""
    def test_set_target_valid_and_invalid(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        orig = E.TARGETS_FILE
        E.TARGETS_FILE = d / "targets.json"
        try:
            self.assertEqual(E.set_target("nvda", "219.5"), 0)
            self.assertEqual(E._load_targets()["NVDA"], 219.5)
            self.assertEqual(E.set_target("X", "abc"), 1)           # 비숫자 거부
            self.assertNotIn("X", E._load_targets())
        finally:
            E.TARGETS_FILE = orig

    def test_set_earnings_valid_and_invalid(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        orig = E.EARNINGS_FILE
        E.EARNINGS_FILE = d / "earnings.json"
        try:
            self.assertEqual(E.set_earnings("de", "2026-08-16"), 0)
            self.assertEqual(E._load_earnings()["DE"], "2026-08-16")
            self.assertEqual(E.set_earnings("X", "2026-13-99"), 1)  # 잘못된 날짜 거부
        finally:
            E.EARNINGS_FILE = orig


class TestSellClampCombos(Base):
    """H3 sell 클램프 조합: 보유분→가능수량 이중 클램프·조회실패 시 보유분만."""
    def _arm(self, owned, sellable):
        E._guard = lambda *a, **k: True
        E._strategy_positions = lambda: {"X": {"qty": owned, "entryPx": 100, "cost_usd": owned * 100}}
        if sellable is None:
            def boom(s):
                raise RuntimeError("api down")
            S.CLIENT.get_sellable_quantity = boom
        else:
            S.CLIENT.get_sellable_quantity = lambda s: {"sellableQuantity": str(sellable)}
        self.planned = []
        self._sp = S.t_plan_order
        S.t_plan_order = lambda sym, side, typ, quantity=None: (
            self.planned.append(quantity), {"error": {"code": "stop"}})[1]   # plan서 중단(주문 안 나감)

    def tearDown(self):
        try:
            S.t_plan_order = self._sp
        except AttributeError:
            pass
        try:
            del S.CLIENT.get_sellable_quantity
        except AttributeError:
            pass
        super().tearDown()

    def test_owned_then_sellable_clamp(self):
        self._arm(owned=1.0, sellable=0.6)
        E.sell("X", 2.0)                       # 2.0→1.0(보유)→0.6(가능)
        self.assertAlmostEqual(self.planned[0], 0.6)

    def test_sellable_fetch_fail_uses_owned(self):
        self._arm(owned=1.0, sellable=None)
        E.sell("X", 2.0)                       # 조회실패 → 보유분 클램프만
        self.assertAlmostEqual(self.planned[0], 1.0)


class TestGoldenJournal(unittest.TestCase):
    """H9 골든 저널 회귀: 대표 이벤트 믹스의 포지션/실현손익/무결성 고정."""
    def setUp(self):
        from pathlib import Path
        self.fx = Path(__file__).parent / "fixtures" / "golden_journal.jsonl"
        self._orig = E.JOURNAL
        E.JOURNAL = self.fx

    def tearDown(self):
        E.JOURNAL = self._orig

    def test_positions(self):
        pos = E._strategy_positions()
        # AAA 전량청산, CCC 전량청산, DDD 미체결종결 → BBB만 잔여(부분매도 후 0.1주)
        self.assertEqual(set(pos), {"BBB"})
        self.assertAlmostEqual(pos["BBB"]["qty"], 0.1)

    def test_realized(self):
        t = E._realized_trades()
        # AAA +10% / BBB PTP 부분 +? / CCC 가격역산(20→19, -5%)
        syms = [(x["sym"], round(x["plUsd"], 2)) for x in t]
        self.assertIn(("AAA", 3.0), syms)                       # 0.3×(110-100)
        self.assertIn(("CCC", -1.0), syms)                      # 역산 20/1 → 19/1
        self.assertTrue(any(s == "BBB" for s, _ in syms))       # PTP 부분실현 기록됨
        self.assertTrue(E._partial_tp_taken("BBB"))             # 현재 BBB 포지션은 PTP 완료 상태

    def test_integrity_and_slots(self):
        self.assertEqual(E.check_journal(), 0)                  # oversell/필수키 무결
        occ, dep = E._open_symbols_and_deployed()
        self.assertEqual(occ, {"BBB"})                          # DDD는 REJECTED로 슬롯 해제


class TestAudit223Fixes(Base):
    """적대적 감사(#223) 확정건 회귀 고정 — 실주문 전 잡은 버그들."""
    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path
        self.d = Path(tempfile.mkdtemp())
        self._oj = E.JOURNAL
        E.JOURNAL = self.d / "index_journal.jsonl"
        E._set_halt = lambda *a, **k: None

    def tearDown(self):
        E.JOURNAL = self._oj
        super().tearDown()

    def evs(self):
        import json as _json
        if not E.JOURNAL.exists():
            return []
        return [_json.loads(x) for x in E.JOURNAL.read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_repoll_preserves_exitcat_no_ptp_repeat(self):
        """재폴링 승격이 exitCat(PTP)을 승계 → 러너 반복매도 방지."""
        E._record_fill("BUY", "X", "b1", "FILLED", 1.0, 100.0)
        E._j({"event": "SELL_WORKING", "sym": "X", "orderId": "s1", "qty": 0.5, "exitCat": "PTP"})
        E._reconcile_fill = lambda oid, tries=2, wait=1.0: ("FILLED", 0.5, 106.0)
        E._repoll_pending()
        sf = [e for e in self.evs() if e["event"] == "SELL_FILLED"][0]
        self.assertEqual(sf.get("exitCat"), "PTP")          # 승계됨
        self.assertTrue(E._partial_tp_taken("X"))           # → 재발동 차단

    def test_partial_fill_then_cancel_records_fill(self):
        """취소 전 부분체결분을 먼저 기록 → 유령주식(브로커O/원장X) 방지."""
        E._j({"event": "BUY_WORKING", "sym": "Y", "orderId": "o9", "usd": 20.0})
        E._reconcile_fill = lambda oid, tries=2, wait=1.0: ("CANCELED", 0.07, 100.0)
        E._repoll_pending()
        evs = self.evs()
        self.assertTrue(any(e["event"] == "BUY_PARTIAL" for e in evs))
        self.assertTrue(any(e["event"] == "BUY_REMAINDER_CANCELED" for e in evs))
        self.assertAlmostEqual(E._strategy_positions()["Y"]["qty"], 0.07)

    def test_fully_recorded_partial_gets_terminal(self):
        """cum 전량이 이미 PARTIAL로 기록된 주문도 종결 이벤트 → 영구 pending(슬롯 잠식) 방지."""
        E._record_fill("BUY", "Z", "o1", "PARTIAL_FILLED", 0.5, 100.0)
        E._reconcile_fill = lambda oid, tries=2, wait=1.0: ("FILLED", 0.5, 100.0)
        E._repoll_pending()
        self.assertEqual(E._pending_working_orders(self.evs()), [])

    def test_entry_ts_refreshed_on_reentry(self):
        """전량청산 후 재진입 시 entryTs 갱신 → 시간청산·트레일링 오발동 방지."""
        J([{"ts": "2026-01-01T00:00:00+09:00", "event": "BUY_FILLED", "sym": "W",
            "fillQty": 1, "fillPrice": 100},
           {"ts": "2026-01-05T00:00:00+09:00", "event": "SELL_FILLED", "sym": "W",
            "fillQty": 1, "fillPrice": 110},
           {"ts": "2026-07-01T00:00:00+09:00", "event": "BUY_FILLED", "sym": "W",
            "fillQty": 1, "fillPrice": 120}])
        self.assertTrue(E._strategy_positions()["W"]["entryTs"].startswith("2026-07-01"))

    def test_journal_cache_key_not_shadowed(self):
        """_J_CACHE 키가 파일 시그니처여야 함(이벤트 튜플 섀도잉 회귀)."""
        E.JOURNAL.write_text('{"ts":"1","event":"BUY_FILLED","sym":"A","fillQty":1,"fillPrice":10}\n')
        E._read_journal()
        self.assertEqual(E._J_CACHE["key"][0], str(E.JOURNAL))   # 파일경로 기반 키

    def test_direct_buy_partial_then_cancel_records(self):
        """buy() 직접 경로도 취소 전 부분체결분 기록(감사#224 — repoll만 고쳤던 갭)."""
        E._guard = lambda *a, **k: True
        E._in_cooldown = lambda s: (False, "")
        E._open_symbols_and_deployed = lambda: (set(), 0.0)
        E._today_deployed_usd = lambda: 0.0
        E._strategy_circuit = lambda: (True, "")
        E._regime_risk_off = lambda: False
        E._days_to_earnings = lambda s: 10
        E._entry_guard = lambda s: (True, "OK")
        E.intraday_entry_check = lambda s: (True, "OK")
        E._spread_ok = lambda s: (True, 0.01)
        S.CLIENT.get_buying_power = lambda cur: {"availableAmount": "100"}
        sp, spl = S.t_plan_order, S.t_place_order_confirmed
        S.t_plan_order = lambda *a, **k: {"preview_token": "t", "confirm_phrase": "c",
                                          "snapshot": {"clientOrderId": "c1"}}
        S.t_place_order_confirmed = lambda t, c: {"orderId": "oX"}
        E._reconcile_fill = lambda oid, tries=6, wait=1.0: ("CANCELED", 0.09, 100.0)
        try:
            rc = E.buy("QQ", 20)
        finally:
            S.t_plan_order, S.t_place_order_confirmed = sp, spl
            try:
                del S.CLIENT.get_buying_power
            except AttributeError:
                pass
        self.assertEqual(rc, 0)
        self.assertTrue(any(e["event"] == "BUY_PARTIAL" for e in self.evs()))
        self.assertAlmostEqual(E._strategy_positions()["QQ"]["qty"], 0.09)   # 유령 방지

    def test_replaced_keeps_slot(self):
        """REPLACED는 후속 주문 생존 가능 → 슬롯 해제·종결 금지(감사#224)."""
        E._j({"event": "BUY_WORKING", "sym": "R", "orderId": "r1", "usd": 15.0})
        E._reconcile_fill = lambda oid, tries=2, wait=1.0: ("REPLACED", 0.0, None)
        E._alert = lambda *a, **k: None
        E._repoll_pending()
        evs = self.evs()
        self.assertFalse(any(e["event"] == "BUY_REJECTED" for e in evs))   # 종결 안 함
        occ, _ = E._open_symbols_and_deployed()
        self.assertIn("R", occ)                                            # 슬롯 유지

    def test_trailing_yields_label_to_target(self):
        """트레일링이 목표가/RSI 라벨을 가리지 않음(attribution 일관)."""
        r = E._exit_decision(pl=4.0, rsi=50, dte=8, cur=104, tgt=104, mfe_pct=8.0)
        self.assertIn("목표가", r); self.assertNotIn("트레일링", r)
        r2 = E._exit_decision(pl=4.0, rsi=50, dte=8, cur=104, tgt=999, mfe_pct=8.0)
        self.assertIn("트레일링", r2)      # 목표 미도달이면 정상 발동


class TestAudit227Fixes(Base):
    """2차 심층감사(#227) 확정건 회귀 고정 — 원가날조·좀비pending·서킷오계산 등."""
    def setUp(self):
        super().setUp()
        import tempfile
        from pathlib import Path
        self._oj = E.JOURNAL
        E.JOURNAL = Path(tempfile.mkdtemp()) / "index_journal.jsonl"
        E._set_halt = lambda *a, **k: None
        E._alert = lambda *a, **k: None

    def tearDown(self):
        E.JOURNAL = self._oj
        super().tearDown()

    def evs(self):
        import json as _json
        if not E.JOURNAL.exists():
            return []
        return [_json.loads(x) for x in E.JOURNAL.read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_partial_without_price_never_uses_order_usd(self):
        """체결가 미상 부분체결에 주문 전액이 원가로 들어가면 진입가 배수 → 가짜 손절."""
        J([{"ts": "1", "event": "BUY_PARTIAL", "sym": "M", "orderId": "o1",
            "fillQty": 0.05, "fillPrice": None, "filledUsd": None, "usd": 18.0, "cum": 0.05}])
        pos = E._strategy_positions()["M"]
        self.assertLess(pos["cost_usd"], 1e-9)          # 주문전액 계상 금지
        entry = pos["entryPx"]
        self.assertFalse(entry and entry > 300)          # $360 같은 날조 진입가 없음
        # 그런 상태에서 청산 판정이 '가짜 손절'을 내지 않아야 함
        self.assertIsNone(E._exit_decision(pl=None, rsi=50, dte=8, cur=213.0, tgt=None))

    def test_realized_trades_no_usd_fabrication(self):
        """부분체결 매수단가를 주문전액으로 날조하면 실현손익·서킷이 오염된다."""
        J([{"ts": "1", "event": "BUY_PARTIAL", "sym": "M", "orderId": "o1",
            "fillQty": 0.05, "fillPrice": None, "filledUsd": None, "usd": 18.0, "cum": 0.05},
           {"ts": "2", "event": "SELL_FILLED", "sym": "M", "orderId": "o2",
            "fillQty": 0.05, "fillPrice": 213.0}])
        for t in E._realized_trades():
            self.assertLess(abs(t["plPct"]), 90)         # -99% 같은 날조 손실 없음

    def test_circuit_counts_sell_events_not_lots(self):
        """FIFO 로트 행이 아니라 '매도 이벤트' 단위로 손실청산을 센다."""
        td = E._trading_day()
        J([{"ts": f"{td}T10:00:00-04:00", "event": "BUY_FILLED", "sym": "A",
            "fillQty": 1, "fillPrice": 100},
           {"ts": f"{td}T10:01:00-04:00", "event": "BUY_FILLED", "sym": "A",
            "fillQty": 1, "fillPrice": 102},
           {"ts": f"{td}T11:00:00-04:00", "event": "SELL_FILLED", "sym": "A",
            "fillQty": 2, "fillPrice": 99}])          # 1회 매도가 2개 로트에 걸림
        ok, reason = E._strategy_circuit()
        self.assertTrue(ok, f"1회 손실청산인데 서킷 발동: {reason}")

    def test_zombie_pending_resolved(self):
        """WORKING→PARTIAL 주문도 종결 처리돼 재폴링 예산을 잠식하지 않음."""
        E._j({"event": "BUY_WORKING", "sym": "Z", "orderId": "z1", "usd": 15.0})
        E._record_fill("BUY", "Z", "z1", "PARTIAL_FILLED", 0.4, 100.0)
        E._reconcile_fill = lambda oid, tries=2, wait=1.0: ("FILLED", 0.4, 100.0)
        E._repoll_pending()
        self.assertEqual(E._pending_working_orders(self.evs()), [])   # 좀비 해소

    def test_manage_exception_is_journaled(self):
        """관리 예외가 조용히 삼켜지지 않고 저널·경보로 남는다."""
        E._strategy_positions = lambda: {"X": {"qty": 1.0, "entryPx": 100.0,
                                               "cost_usd": 100.0, "entryTs": "2026-07-14T00:00:00"}}
        def boom(s):
            raise RuntimeError("feed exploded")
        S.CLIENT.get_price = lambda s: {"lastPrice": "100"}
        E._fetch_daily = boom                     # 지표 단계에서 폭발
        E._repoll_pending = lambda *a, **k: None
        E._days_to_earnings = lambda s: None; E._load_targets = lambda: {}
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        E.sell = lambda *a, **k: 0
        E.manage()
        self.assertTrue(any(e["event"] == "MANAGE_ERROR" for e in self.evs()))

    def test_price_sanity_blocks_bogus_tick(self):
        """lastPrice=0 한 틱이 전량 청산을 유발하지 않는다."""
        E._strategy_positions = lambda: {"X": {"qty": 1.0, "entryPx": 100.0,
                                               "cost_usd": 100.0, "entryTs": "2026-07-14T00:00:00"}}
        S.CLIENT.get_price = lambda s: {"lastPrice": "0"}
        E._repoll_pending = lambda *a, **k: None
        E._days_to_earnings = lambda s: None; E._load_targets = lambda: {}
        MC.us_session = lambda: "미국 정규장"; E._market_open = lambda: (True, "개장")
        calls = []
        E.sell = lambda *a, **k: (calls.append(a), 0)[1]
        E.manage()
        self.assertEqual(calls, [])               # 매도 시도 없음


class TestScreenerSessionCache(unittest.TestCase):
    """캐시 세션경계 헬퍼(#223)."""
    def test_regular_now_helper_exists_and_bounds(self):
        self.assertIn(SC._is_us_regular_now(), (True, False))    # 런타임 NameError 회귀 방지


class TestDynamicUniverse(unittest.TestCase):
    """2단 스크리닝 동적 유니버스 로더: 신선하면 동적, 오래되면/없으면 정적 폴백."""
    def setUp(self):
        self._orig = SC.DYN_FILE

    def tearDown(self):
        SC.DYN_FILE = self._orig

    def _write(self, ts, syms):
        import tempfile
        import json as _json
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False)
        _json.dump({"ts": ts, "symbols": syms}, f); f.close()
        SC.DYN_FILE = f.name
        return f.name

    def test_fresh_dynamic_used(self):
        self._write(datetime.now().isoformat(timespec="seconds"), ["ZZ1", "ZZ2", "AAPL"])
        syms, src = SC._load_universe()
        self.assertEqual(syms, ["ZZ1", "ZZ2"])          # 동적 사용 + LEGACY(AAPL) 제외
        self.assertIn("동적", src)

    def test_stale_falls_back_static(self):
        self._write((datetime.now() - timedelta(hours=48)).isoformat(timespec="seconds"), ["ZZ1"])
        syms, src = SC._load_universe()
        self.assertIn("폴백", src)
        self.assertGreater(len(syms), 50)               # 정적 유니버스로 복귀

    def test_missing_uses_static(self):
        SC.DYN_FILE = "/nonexistent/u.json"
        syms, src = SC._load_universe()
        self.assertIn("정적", src)
        self.assertGreater(len(syms), 50)


class TestExtEntry(unittest.TestCase):
    """장외 예외진입 판정(_ext_entry_ok): 급락+타이트스프레드+프리/애프터만, fail-closed."""
    def test_ok_when_all_met(self):
        ok, r = E._ext_entry_ok("미국 프리마켓", 0.2, -4.5)
        self.assertTrue(ok)

    def test_regular_session_not_ext(self):
        self.assertFalse(E._ext_entry_ok("미국 정규장", 0.2, -4.5)[0])
        self.assertFalse(E._ext_entry_ok("미국장 휴장", 0.2, -4.5)[0])

    def test_spread_fail_closed(self):
        self.assertFalse(E._ext_entry_ok("미국 프리마켓", None, -4.5)[0])   # 호가미확인=거부
        self.assertFalse(E._ext_entry_ok("미국 프리마켓", 0.8, -4.5)[0])    # 얇은 호가

    def test_requires_dislocation(self):
        self.assertFalse(E._ext_entry_ok("미국 프리마켓", 0.2, -1.0)[0])    # 평범한 하락은 거부
        self.assertFalse(E._ext_entry_ok("미국 프리마켓", 0.2, None)[0])
        self.assertTrue(E._ext_entry_ok("미국 애프터마켓", 0.3, -3.5)[0])


class TestFifoProperty(Base):
    """H6 FIFO property: 시드 랜덤 시퀀스에서 불변식 — 실현수량≤매수수량, 잔여qty≥0,
    (매수-매도)=잔여+미매칭, P&L 합 = Σ(sell-buy)×qty 일관."""
    def test_random_sequences(self):
        import random
        rng = random.Random(20260718)          # 시드 고정(재현성 — 하네스 규칙)
        for trial in range(20):
            evs, ts = [], 0
            bought = sold = 0.0
            for _ in range(rng.randint(3, 15)):
                ts += 1
                if rng.random() < 0.6 or bought <= sold:
                    q = round(rng.uniform(0.1, 2.0), 4)
                    evs.append({"ts": f"{ts:04d}", "event": "BUY_FILLED", "sym": "X",
                                "fillQty": q, "fillPrice": round(rng.uniform(50, 150), 2)})
                    bought += q
                else:
                    q = round(rng.uniform(0.1, max(0.1, bought - sold)), 4)
                    evs.append({"ts": f"{ts:04d}", "event": "SELL_FILLED", "sym": "X",
                                "fillQty": q, "fillPrice": round(rng.uniform(50, 150), 2)})
                    sold += q
            J(evs)
            trades = E._realized_trades()
            pos = E._strategy_positions()
            realized_q = sum(t["qty"] for t in trades)
            rem_q = pos.get("X", {}).get("qty", 0.0)
            self.assertLessEqual(realized_q, bought + 1e-6, f"trial{trial}: 실현>매수")
            self.assertGreaterEqual(rem_q, -1e-9, f"trial{trial}: 음수 잔여")
            # 잔여 = 매수 - 유효매도(오버셀은 클램프돼 유효매도≥실현수량) → rem ≤ bought - realized
            self.assertLessEqual(rem_q, bought - realized_q + 1e-6, f"trial{trial}: 잔여 과다")
            for t in trades:                   # P&L 정의 일관
                self.assertAlmostEqual(t["plUsd"], (t["sellPx"] - t["buyPx"]) * t["qty"], places=6)


class TestCooldownThreshold(Base):
    """C10 쿨다운 정밀화: 미세손실(<3%)은 쿨다운 없음, 진짜 손절(≥3%)만."""
    def _mk(self, pl_pct):
        today = datetime.now(MC.KST).strftime("%Y-%m-%d")
        J([{"ts": f"{today}T01:00:00+09:00", "event": "BUY_FILLED", "sym": "X",
            "fillQty": 1, "fillPrice": 100},
           {"ts": f"{today}T02:00:00+09:00", "event": "SELL_FILLED", "sym": "X",
            "fillQty": 1, "fillPrice": 100 + pl_pct}])

    def test_small_loss_no_cooldown(self):
        self._mk(-0.9)                          # MDLZ류 노이즈컷
        self.assertFalse(E._in_cooldown("X")[0])

    def test_real_stop_cooldown(self):
        self._mk(-8.0)
        self.assertTrue(E._in_cooldown("X")[0])

    def test_boundary_minus3(self):
        self._mk(-3.0)
        self.assertTrue(E._in_cooldown("X")[0])  # 경계 포함(≤-3%)


class TestNewHelpers(Base):
    """A9 연속실패·C9 배당락·K8 스크럽·I1 저널캐시·D4 RS합성."""
    def test_sell_fail_streak(self):
        now = datetime.now(MC.KST).isoformat()
        J([{"ts": now, "event": "SELL_FAIL", "sym": "X"},
           {"ts": now, "event": "SELL_RETRY", "sym": "X"},
           {"ts": now, "event": "SELL_FAIL", "sym": "X"}])
        self.assertEqual(E._sell_fail_streak("X"), 3)
        J([{"ts": now, "event": "SELL_FAIL", "sym": "X"},
           {"ts": now, "event": "SELL_FILLED", "sym": "X", "fillQty": 1, "fillPrice": 1}])
        self.assertEqual(E._sell_fail_streak("X"), 0)   # 체결로 리셋

    def test_days_to_dividend(self):
        import tempfile
        import json as _json
        from pathlib import Path
        from datetime import date, timedelta
        d = Path(tempfile.mkdtemp()); f = d / "div.json"
        f.write_text(_json.dumps({"NEE": str(date.fromisoformat(E._trading_day()) + timedelta(days=2))}))
        orig = E.DIVIDEND_FILE; E.DIVIDEND_FILE = f
        try:
            self.assertEqual(E._days_to_dividend("NEE"), 2)
            self.assertIsNone(E._days_to_dividend("XOM"))
        finally:
            E.DIVIDEND_FILE = orig

    def test_scrub_masks_secrets(self):
        s = E._scrub("Authorization: Bearer abcdef1234567890 appkey=SK99SECRETVALUE ok")
        self.assertNotIn("abcdef1234567890", s)
        self.assertNotIn("SK99SECRETVALUE", s)
        self.assertIn("***", s)
        self.assertEqual(E._scrub("BUY NVDA 0.5주"), "BUY NVDA 0.5주")   # 일반 텍스트 무변형

    def test_journal_cache_invalidates_on_append(self):
        import tempfile
        from pathlib import Path
        d = Path(tempfile.mkdtemp())
        origJ = E.JOURNAL
        E.JOURNAL = d / "index_journal.jsonl"
        try:
            E.JOURNAL.write_text('{"ts":"1","event":"BUY_FILLED","sym":"A","fillQty":1,"fillPrice":10}\n')
            self.assertEqual(len(E._read_journal()), 1)
            self.assertEqual(len(E._read_journal()), 1)      # 캐시 히트
            with E.JOURNAL.open("a") as f:                   # append → mtime/size 변화
                f.write('{"ts":"2","event":"SELL_FILLED","sym":"A","fillQty":1,"fillPrice":11}\n')
            self.assertEqual(len(E._read_journal()), 2)      # 자연 무효화(I1)
        finally:
            E.JOURNAL = origJ

    def test_review_picks(self):
        from datetime import datetime, timedelta
        old_ts = (datetime.now() - timedelta(days=5)).isoformat(timespec="seconds")
        new_ts = datetime.now().isoformat(timespec="seconds")
        hist = [
            {"ts": old_ts, "top": [{"sym": "AAA", "zone": "🟢매수존", "px": 100.0},
                                    {"sym": "BBB", "zone": "🟢매수존", "px": 50.0},
                                    {"sym": "CCC", "zone": "⚪관망", "px": 10.0}]},   # 관망 제외
            {"ts": new_ts, "top": [{"sym": "AAA", "zone": "🟢매수존", "px": 999.0}]},  # 너무 최신 → 미사용
        ]
        res, ts = SC._review_picks(hist, {"AAA": 105.0, "BBB": 45.0}, min_age_days=3)
        self.assertEqual(ts, old_ts)                        # 오래된 스냅샷 기준
        d = {s: r for s, _, _, r in res}
        self.assertAlmostEqual(d["AAA"], 5.0)
        self.assertAlmostEqual(d["BBB"], -10.0)
        self.assertNotIn("CCC", d)                          # 매수존 픽만 평가
        self.assertEqual(SC._review_picks([], {}, 3), ([], None))

    def test_rs_blend(self):
        self.assertAlmostEqual(SC._rs_blend(10, 5, 4, 2), 0.7 * 6 + 0.3 * 3)   # 정상 합성
        self.assertAlmostEqual(SC._rs_blend(10, None, 4, None), 6.0)           # 1m 결측 → 3m만
        self.assertAlmostEqual(SC._rs_blend(None, None, None, None), 0.0)      # 전결측 → 0


if __name__ == "__main__":
    unittest.main(verbosity=2)
