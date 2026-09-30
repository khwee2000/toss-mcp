#!/bin/zsh
# weekly.sh — 주간 학습 리뷰 원커맨드(J7 실용판).
# 실현손익·룰별 성과 → 에쿼티 추세 → 스크리너 적중률 → 원장 대사 → Serenity 다이제스트.
PY=python3.12
cd "$(dirname "$0")"
echo "════════════ 주간 리뷰 $(date +%F) ════════════"
$PY execute.py --report        2>/dev/null | grep -v "INFO\|WARNING"
echo ""
$PY execute.py --equity        2>/dev/null | grep -v "INFO\|WARNING" | tail -8
echo ""
$PY stock_screener.py --review 7 2>/dev/null | grep -v "INFO\|WARNING"
echo ""
$PY execute.py --verify-broker 2>/dev/null | grep -v "INFO\|WARNING" | tail -6
echo ""
$PY serenity_check.py --digest 2>/dev/null | grep -v "INFO\|WARNING" | head -14
