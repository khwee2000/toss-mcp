# 🚨 toss-mcp 실전 운영 런북

## 긴급 정지 (즉시, 순서대로)
```bash
touch ~/.toss-mcp/HALT                 # 1) 모든 주문 차단 (재시작 불필요, 매 POST 재확인)
# .env에 TOSS_KILL=1                   # 2) 강제 mock 강등 + 전 주문 거부
rm ~/.toss-trader/LIVE_GO              # 3) 실행기(live_trade) 게이트 잠금
```
해제: HALT 파일 삭제 / TOSS_KILL 비움 / LIVE_GO 재생성(재검수 GO 후에만).

## 비상 전량청산 (전략 포지션만, legacy 미접촉)
```bash
python3.12 ~/toss-mcp/live_trade.py --panic-exit
```

## 상태 확인
```bash
python3.12 market_cycle.py             # 사이클 리포트 (regime·신호·리스크)
python3.12 live_trade.py --status      # 실포지션·미체결 reconcile
python3.12 market_cycle.py --report    # 성과 (승률·PF·시그널별)
tail ~/.toss-trader/journal.jsonl      # 결정 저널
tail ~/.toss-trader/audit.jsonl        # 주문 감사로그
```

## 장애 대응
| 증상 | 조치 |
|---|---|
| 주문 TIMEOUT/pendingFill | `live_trade.py --status` (자동 reconcile). 미해결 시 토스앱에서 주문 확인 |
| reconcile-required 에러 | **재시도 금지.** track_order로 주문 상태 확정 후 판단 |
| DUPLICATE 거부 | 5분(dedup window) 대기 후 재시도, 또는 기존 주문 확인 |
| 상태 체크섬 MISMATCH | paper_portfolio.json 수동 수정 여부 확인, `--undo`로 복원 가능 |
| auto-HALT (10분 4건) | 이상거래 검토 후 `rm ~/.toss-mcp/HALT` |
| 사이클 락 걸림 | 이전 프로세스 종료 확인 후 `rm ~/.toss-trader/cycle.lock` |
| 서킷 TRIP (일 -5pt/낙폭 -15pt) | 당일 중단. 다음날 자동 비중 반감. 원인 복기 후 재개 |

## 한도 (현재 설정)
건당 ₩50만 / 일 ₩100만·10건 / 실행기: 1주문 ≤$60·실포지션 ≤2·일 진입 3·주 8
손절 −7%(ATR 적응 트레일) / 익절 +5% 절반·+10% 잔량 / 20일 무진전 청산

## 세션 종료 시
cron 루프(세션 한정)는 Claude 종료와 함께 멈춤 — **포지션에 손절 주문이 자동으로 남지 않음**.
장기 무인 운영 전엔 반드시: 포지션 정리 또는 토스앱에 수동 손절 예약.
