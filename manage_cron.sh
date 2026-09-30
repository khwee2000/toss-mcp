#!/bin/zsh
# manage_cron.sh — OS 레벨 매도보호 크론(A안, 2026-07-20 사용자 승인).
# Claude 세션과 무관하게 보유 포지션의 청산룰(손절·목표·트레일링·실적전청산)을 실행.
# 신규 매수는 하지 않음(manage는 매도 전용) — 반자동 원칙 유지.
# execute.lock이 세션 사이클과의 동시실행을 자동 방지(겹치면 이쪽이 스킵).
cd ~/toss-mcp
export TOSS_DESKTOP_ALERTS=1   # 장외 청산신호 등 크리티컬 알림을 macOS 알림으로 표시(감사#238)
LOG=~/.toss-trader/oscron.log
# 로그 5MB 로테이션
[ -f "$LOG" ] && [ $(stat -f%z "$LOG" 2>/dev/null || echo 0) -gt 5000000 ] && mv "$LOG" "$LOG.1"
echo "── $(date '+%F %T') manage(OS cron)" >> "$LOG"
python3.12 execute.py --manage >> "$LOG" 2>&1
