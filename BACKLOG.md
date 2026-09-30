# 개선 백로그 (야간 100개 작업) — 감사 5개 에이전트 발굴

상태: [ ]대기 [x]완료 [~]검증됨 [skip]보류. 각 구현 후 `python3.12 tests/test_index_executor.py` 회귀 확인 필수.
원칙: 안정성 최우선, 하나하나 검증, 검증된 주문경로(server/safety)는 신중히. 대부분 additive.

## Wave A — 테스트 그물 확장 (additive, risk L) ✅ 1차 19개 완료
- [x] A0 test_index_executor.py 19케이스(positions·realized·circuit·reconcile·targets)
- [x] A1 manage() 청산 우선순위 테스트(earnings>stop>target>RSI) [T7]
- [x] A2 manage() tradable 게이팅(장 닫히면 sell 안 부름) [T8]
- [x] A3 _days_to_earnings 경계(today=0·과거<0·미상None·malformed None) + block 조건 [T9]
- [ ] A4 _j 내구성(critical 실패→fallback+HALT, 비critical 무시) [T11]
- [x] A5 intraday_entry_check 게이트(day>2.5·None·m3>1.5·m3<-2·정상) [T12]
- [ ] A6 screener analyze 존분류(매수존/관망/추세깨짐/과열제외) [T13]
- [ ] A7 screener 부분봉 제외 재현성(c[:-1]) [T14]
- [ ] A8 screener 커버리지<70% exit3 [T15]
- [ ] A9 screener 보유/레거시 제외 [T16]

## Wave B — 관측성 (additive, high value) 
- [x] B1 --status 헬스체크(세션·개장·HALT·포지션·예산·서킷·저널) [T20]
- [x] B2 --check-journal 무결성 검증(스키마·oversell·ts단조) [T21]
- [ ] B3 구조적 logging(레벨·KST타임스탬프·~/.toss-trader/execute.log) [T17] risk M
- [x] B4 --report에 최대낙폭·Sharpe·기대값 추가 [T18]
- [x] B5 종목별 청산사유(STOP/TARGET/RSI/EARN) 저널링+report 분해 [T19]
- [ ] B6 --dry-run 페이퍼 실행경로(실주문 대체·PAPER저널) [T23] risk M
- [x] B7 포트폴리오 일일손실·데이터실패 알림+fail-closed HALT [T22] risk M

## Wave C — 전략/리스크 (behavior, verify) — 대부분 sibling 순수함수 배선
- [x] C1 과열-기대감 진입차단: RSI≥62 & MA150 +8%↑ 이면 buy 거부 [S14] ★사용자 인사이트
- [ ] C2 레짐 게이트: SPY/QQQ 200MA 아래(risk_off)면 신규 차단 [S7]
- [ ] C3 트레일링 스탑: highwater 추적 + trading_rules.trailing_stop [S1]
- [x] C4 브레이크이븐 스탑: +4% 후 손절선=진입가 [S2]
- [ ] C5 부분 익절(scale-out): 목표/RSI시 절반 SELL_PARTIAL+나머지 트레일 [S3]
- [x] C6 시간청산: days_held>20 & 진전<3% 이면 정리 [S4]
- [x] C7 추세이탈 청산: cur<MA150 이면 -8% 전에 청산 [S5]
- [x] C8 재진입 쿨다운: 손절 후 2일내 동일종목 재매수 차단 [S6]
- [x] C9 포트폴리오 $손실 서킷: 오늘 실현손실 ≤ -X% 배치자본 이면 신규중단 [S9]
- [ ] C10 미상-실적 갭가드: earnings None이면 신규차단 또는 타깃+타이트스탑 [S17]
- [x] C11 진입 논지 재검증: buy()에서 RSI38-58·>MA150·비과열 독립확인 [S15]
- [x] C12 변동성 스탑: STOP=min(-8, -(1.5*ATR%)) [S12]
- [ ] C13 현금예비: BUDGET_USD 78→~70(예비 확보) [S18]
- [ ] C14 갭관통 에스컬레이션: pl≤-10%면 ALERT+즉시 시장가 청산 [S19]
- [ ] C15 동적 RSI-과열: TP_RSI 레짐/ATR 적응(risk_on 68·risk_off 62) [S16]
- [ ] C16 피크에쿼티 낙폭 -15% 신규중단 [S10]
- [ ] C17 ATR 기반 사이징 [S11]
- [ ] C18 변동성 스케일 타깃(targets 없으면 entry*(1+2.5*ATR%)) [S13]
- [ ] C19 pre-earnings 청산 마지막 세션 보장(dte==1 종가부근) [S20]
- [ ] C20 섹터/상관 집중 캡(≥3 한섹터 거부·corr>0.85 거부) [S8]

## Wave D — 스크리너/선정 (behavior, verify)
- [ ] D1 정지/경고종목 제외 get_stock_warnings [SC19]
- [ ] D2 유동성 하한(거래대금 20일 중앙값) [SC1]
- [x] D3 상대강도 vs QQQ 점수 반영 [SC5]
- [x] D4 낙폭(falling-knife) 가드: ret6m 활용 [SC7]
- [x] D5 실적임박 점수화(5-12일 보너스·0-2일 제외) [SC8]
- [x] D6 추세깨짐(up=False) 랭킹서 제외 [SC20]
- [x] D7 볼륨확인(눌림이 저볼륨) [SC2]
- [ ] D8 페이지네이션 fetch(≥252봉) [SC11]
- [ ] D9 지수백오프+지터 재시도 [SC12]
- [x] D10 신선도 검출(마지막봉 stale) [SC13]
- [ ] D11 52주 컨텍스트(고점부근 페널티) [SC9]
- [ ] D12 섹터 분산 랭킹(섹터당 캡·미보유섹터 가점) [SC3]
- [ ] D13 상관 필터 vs 보유 [SC4]
- [ ] D14 지지-저항 리스크리워드 점수 [SC6]
- [x] D15 갭 처리(|gap|>3% 제외) [SC10]
- [ ] D16 멀티타임프레임(주봉 확인) [SC14]
- [ ] D17 ATR 횡단면 정규화 [SC15]
- [ ] D18 워치리스트 히스토리 저장 screener_history.jsonl [SC16]
- [ ] D19 UNIVERSE/LEGACY 외부 JSON 단일화 [SC17]
- [ ] D20 IND.compute 리치필드 재사용(arrange·slopeDir·pos52) [SC20b]

## Wave E — money-safety (실전 위험, 신중)
- [x] E5 단일 실행 락 fcntl.flock(이중매도 방지) [MS5] ✅
- [x] E7 sell DUPLICATE exid없으면 HALT(buy와 대칭) [MS7] ✅
- [x] E-cx1 DUPLICATE existing_order_id 최상위 r에서 읽기(buy·sell) [CX1] ✅
- [ ] E1 PARTIAL_FILLED → BUY_PARTIAL/SELL_PARTIAL 기록 + orderedQty/remaining + 다음사이클 재reconcile [MS1/MS2/CX7/CX22] H
- [x] E3 _open_symbols_and_deployed 부분매도 순qty 반영(전체제거 X) [MS3/CX2] H risk M
- [ ] E4 manage() 프리패스: BUY_WORKING/UNCERTAIN 재폴링→체결시 BUY_FILLED 승격 [MS4] H
- [ ] E6 유휴 working 주문 idempotency: 시간창 대신 '심볼 오픈주문 존재'로 [MS6] H
- [x] E8 sell qty를 min(sellable, 전략보유qty)로 클램프+전략미보유 거부 [MS8/CX18] H
- [x] E9 _read_journal에 fallback.jsonl 병합(startup/--reconcile) [MS9/CX8] H
- [ ] E10 --reconcile 명령: uncertain 주문 client_order_id로 폴링→기록→HALT해제 [MS10] H
- [x] E11 _recent_trade 타임스탬프 tz 견고화(Z/epoch/offset·naive처리) [MS11/CX12] H risk M
- [x] E12 _market_open 프록시 바스켓(NVDA단독 X → SPY/QQQ/AAPL any) [MS12/CX13] M
- [ ] E14 working 주문 타임아웃(N분 후 취소-or-reconcile) [MS14] M
- [x] E15 409 idempotent/빈 get_order → orderId None이면 phantom 대신 HALT [MS15] M
- [x] E16 저널 파일 권한 0600(os.open 0o600+chmod) [MS16] M
- [x] E17 저널 이벤트에 clientOrderId(+preview_token 해시) 스탬프 [MS17] M
- [x] E18 execute gate에서 buying_power 재확인(BUY) [MS18] M
- [ ] E19 _reconcile_fill 조회실패→UNKNOWN 구분, 전부실패시 HALT(미체결 가정 X) [MS19/CX19] M
- [ ] E20 일일 KRW 카운터를 실체결 notional로 [MS20] L
- [ ] E21 FX/KRW 정산 반영한 예산 추적 [MS21] L

## Wave F — correctness 버그 (하나하나 검증)
- [x] F3 서킷 KST date→ET 거래일 버킷(자정후 리셋버그) [CX3] H risk M
- [ ] F4 us_session ET-aware(EDT/EST) zoneinfo [CX4] M (겨울에 깨짐)
- [x] F5 _realized_trades: 가격미상 SELL은 lot 소비 안 함 [CX5] M
- [x] F6 "0.00" 문자열 fillPrice=0 방지(float>0 검증) [CX6] M
- [x] F10 _load_targets 키별 try/except(하나 깨져도 나머지 유지) [CX10] M
- [x] F11 _days_to_earnings ET date 비교(off-by-one) [CX11] M
- [x] F14 _day_change 부분봉 판정(개장직전 dc[-1]=어제종가 케이스) [CX14] M
- [x] F15 screener c[:-1] 정확히 155봉 경계 [CX15] L
- [x] F16 manage _rsi 완료봉으로(스크리너와 일관) [CX16] L
- [x] F17 _strategy_positions qty≥0 클램프+경고 [CX17] L
- [x] F20 screener RSI 표시-게이트 반올림 일치 [CX20] L
- [x] F21 intraday m3 완료봉 기준 [CX21] L
