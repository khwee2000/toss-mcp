# 2라운드 백로그 — 진화된 코드 재감사 (2026-07-15 17시)
상태 [ ]대기 [x]완료. 각 후 tests/test_index_executor.py 회귀+py_compile 필수.

## 테스트 공백 (risk L, 순수 additive — 우선)
- [x] A13 이중계산 버그 수정+테스트 ✅
- [x] TA1 manage() 포트폴리오 요약/PORTFOLIO_ALERT 계산 테스트
- [x] TA2 manage() 시세실패 중립 회계 테스트
- [x] TA3 manage() 다종목 다규칙 end-to-end(STOP/RSI/hold + reason_cat 전달)
- [x] TA4 _exit_category: 추세이탈/브레이크이븐/시간청산 → OTHER
- [x] TA5 sell() 이중클램프(전략보유∩sellable) — t_plan_order quantity 확인
- [x] TA6 sell() 전략미보유·레거시 거부
- [x] TA7 buy() 가드순서(금액 조기차단·물타기 서킷보다 먼저)
- [x] TA9 _reconcile_fill PARTIAL_FILLED 비terminal(3회 폴링·partial 반환)
- [x] TA10 _mfe_pct 날짜필터(진입전 제외·None·경계)
- [x] TA11 _read_journal fallback단독+malformed/blank skip
- [x] TA14 _report_stats pf=inf·sharpe
- [x] TA15 _market_open 백스톱(휴장+체결→open·휴장+무체결→closed·정상)
- [x] TA16 status() exit코드 매트릭스
- [x] TA17 check_journal 무결성(oversell·ts역행·필수키)

## 남은 백로그 재확인 (안전순)
- [ ] RB1 E1 PARTIAL_FILLED→BUY_PARTIAL 기록+remaining+재reconcile [MS1] M
- [ ] RB2 E4 manage 프리패스 working/uncertain 재폴링→승격 [MS4] M (A13선행 완료)
- [ ] RB3 E19 _reconcile_fill 전부실패→UNKNOWN 구분·HALT [MS19] M
- [x] RB4 F4 us_session ET-aware zoneinfo(겨울 EST) [CX4] M
- [x] RB5 C2 레짐 게이트(SPY/QQQ<200MA 신규차단) [S7] L-M
- [x] RB6 C10 미상실적 갭가드(earnings None→차단) [S17] L
- [x] RB7 C16 피크에쿼티 -15% 신규중단 [S10] L
- [x] RB8 C19 pre-earnings 마지막세션 청산 보장 [S20] L-M
- [skip] RB9(80콜 비용+주문경로가 이미 정지종목 차단) D1 정지/경고종목 제외 get_stock_warnings [SC19] L
- [x] RB10 D2 유동성 하한(20일 거래대금 중앙값) [SC1] L
- [x] RB11 D8 페이지네이션 fetch≥252봉(_ma150 의존) [SC11] L
- [x] RB12 D9 지수백오프+지터 재시도 [SC12] L
- [x] RB13 B3 구조적 로깅 execute.log [T17] L-M
- [x] RB14 C8b/C14 갭관통 -10% ALERT+즉시청산 [S19] M
- [x] RB15 E10 --reconcile 명령 [MS10] M

## 신규 관측/견고성 (에이전트 제안)
- [x] N1 --verify-broker(저널원장 vs get_holdings 드리프트) 
- [x] N2 --backtest-strategy(저널+과거봉으로 청산룰 what-if)
- [x] N3 heartbeat/staleness watchdog(last_run)
- [x] N4 크리티컬 이벤트 out-of-band 알림
- [x] N5 --selftest 프리플라이트 게이트
- [x] N6 metrics.jsonl 일일 에쿼티 스냅샷

## (execute정확성·money-safety·스크리너 에이전트 3개 결과 추가 예정)

## === 4개 감사 종합 추가 (risk 태그) ===

## risk-H 클러스터 (money 기록 — 루프 금지, 신중히 직접만)
- [ ] MH1 체결가미상("0.00")→포지션/실현손익/서킷/쿨다운 오염: filledUsd/fillQty로 가격 역산·모든 경로 일관 [exec#1-3,MS#3/14] H·H
- [ ] MH2 PARTIAL_FILLED→BUY_PARTIAL/SELL_PARTIAL·terminal까지 재reconcile [E1,MS#1/8,exec#9] H·H
- [ ] MH3 stale-coid DUPLICATE 재체결 이중롱: orderId로 저널 collapse(한 주문 한 terminal) [MS#2] H·M
- [ ] MH4 reconcile 전부실패→UNKNOWN·HALT(미체결 가정 금지) buy·sell [E19,MS#4/6,exec#10] H·M
- [ ] MH5 E4 manage 프리패스 working/uncertain 재폴링→승격 [MS#7,exec#11] H·M
- [ ] MH6 SELL DUPLICATE가 CANCELED주문 채택→손절 삼킴: terminal이면 재plan [MS#18] M·M
- [ ] MH7 _read_journal 재기록 이중(ts다름)→orderId/coid로 dedup [MS#2,exec#20] M·M
- [ ] MH8 MCP 주문경로가 락·저널 밖(교차 이중매수/coid충돌) [MS#10] M·M

## risk-L/M 안전 (루프 가능)
- [x] SL1 sell reconcile-all-fail HALT(exec#10 buy와 대칭) M
- [x] SL2 manage 시세실패시 earnings 청산은 평가(가격불필요) [exec#16] M
- [x] SL3 _days_held 거래일 기준 or 임계 상향(캘린더일 20≈28) [exec#17] L
- [x] SL4 포트폴리오 요약 매도분 제외(이중계산) [exec#18] L
- [x] SL5 check_journal이 merged view(fallback포함) [exec#19,MS#16] L
- [x] SL6 트렌드브레이크 pl>0시 target/RSI 라벨 우선(attribution) [exec#25] L
- [x] SL7 manage 190봉 1회 fetch로 rsi/ma150/atr/mfe 통합(20→5콜) [exec#7] L
- [x] SL8 sell에 skip_market_check 전달(중복 개장체크) [exec#8] L
- [x] SL9 _mfe_pct ET날짜 비교(_trading_day)·완료봉close 기반 브레이크이븐 [exec#4/6] M
- [x] SL10 config KRW 한도 전략스케일(~90k/110k) 하드백스톱 [MS#11] M
- [x] SL11 _j 전부실패시 hard-exit(무기록 매매금지) [MS#12] L
- [x] SL12 intraday <5분봉 fail-closed(현재 통과) [exec#15] M
- [x] SL13 _qqq_ret3m 성공시만 캐시+RS비활성 경고 [SC6/18] L
- [x] SL14 스크리너 부분봉 판정(ET today==최신봉일 때만 c[:-1]) [SC2/23] M
- [x] SL15 스크리너 실적 3~4일도 제외(near_earn dte<=4) [SC5] M
