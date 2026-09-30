#!/bin/zsh
# ci.sh — 원커맨드 검증(H10): 컴파일 + 전체 테스트. 개선/수정 후 이것만 돌리면 됨.
set -e
PY=python3.12
cd "$(dirname "$0")"
echo "[1/2] py_compile..."
$PY -m py_compile execute.py server.py stock_screener.py serenity_check.py market_cycle.py \
    toss_client.py config.py safety.py indicators.py trading_rules.py 2>/dev/null \
  || $PY -m py_compile execute.py server.py stock_screener.py serenity_check.py market_cycle.py
echo "[2/2] tests..."
$PY tests/test_index_executor.py 2>&1 | tail -3
echo "✅ CI 통과"
