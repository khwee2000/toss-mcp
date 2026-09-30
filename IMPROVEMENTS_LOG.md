# 야간 개선 로그 (목표 100개, 아침 8시까지)

각 항목: 상태(✅완료·🔬검증됨·⏳진행·⏭️보류) · 파일 · 개선 · 검증방법

## 이미 완료(오늘 낮, 감사 top-20 + 추가)
1. ✅ 레거시 종목 buy/sell/스크리너 완전차단
2. ✅ 체결확인 reconcile(execution 하위 파싱 수정)
3. ✅ fail-closed 진입게이트
4. ✅ 물타기 차단
5. ✅ 예산/종목수 상한
6. ✅ _day_change 날짜버그 수정
7. ✅ reconcile-required 고아주문 방지
8. ✅ place 직전 HALT 재확인
9. ✅ sell 매도가능수량 클램프
10. ✅ 휴장일 캘린더 게이트 → 실체결 백스톱으로 개선
11. ✅ 코드화 manage(-8%손절/RSI65/목표가/실적청산)
12. ✅ 실적일 연동
13. ✅ 일일 서킷(진입/손실청산)
14. ✅ 저널 내구성(fallback+HALT)
15. ✅ DUPLICATE 분기
16. ✅ --report 실현손익/승률/PF
17. ✅ holdings 평단/손익/레거시태그
18. ✅ 매수 스프레드 가드
19. ✅ 스크리너 예외격리
20. ✅ 커버리지<70% 매매금지
21. ✅ 보유종목 제외 + 실USD가
22. ✅ MARKET_CLOSED 자체버그(check_market_open) 실체결 백스톱 수정
23. ✅ 목표가 코드화(targets.json + manage 배선)
24. ✅ 스카우팅/워치리스트 크론

## 야간 배치 (23번부터 이어서)
25. ✅🔬 tests/test_index_executor.py — 19 단위테스트(positions·realized FIFO·circuit·reconcile·targets). 검증:직접실행 19 pass
26. ✅🔬 단일 실행 락 fcntl.flock(--buy/--sell/--manage) — 크론 겹침 이중주문 방지 [MS5]. 검증:컴파일+동작
27. ✅🔬 --status 헬스체크(세션·개장·HALT·포지션·예산·서킷·저널) [T20]. 검증:실행 exit0(정상)/1(문제)
28. ✅🔬 --check-journal 무결성(필수키·oversell·ts단조) [T21]. 검증:실행 12줄 0문제
29. ✅🔬 DUPLICATE existing_order_id 최상위 r에서 읽기(buy·sell) — 내가 넣은 버그, safety.py 확인 [CX1/MS7]. 검증:safety.py _err 최상위 확인
30. ✅🔬 sell DUPLICATE exid없으면 HALT(buy와 대칭·무해성공 방지) [MS7]. 검증:컴파일+테스트
31. ✅🔬 --status exit코드 정밀화(진입상한=정상, HALT/fallback/손실서킷만 unhealthy). 검증:exit0 확인
32. ✅🔬 _load_targets 키별 파싱(한 종목 값 깨져도 나머지 목표가 유지) [CX10]. 검증:test_bad_value_skipped
33. ✅🔬 _reconcile_fill "0.00" 체결가→미상 취급(fp=0 P&L오염 방지) [CX6]. 검증:test_zero_price_treated_unknown
34. ✅🔬 _market_open 프록시 바스켓(AAPL/MSFT/NVDA/SPY any) — 단일종목 halt에도 개장판정 [MS12/CX13]. 검증:컴파일+테스트

## 야간 루프 배치 2 (02:2x KST)
35. ✅🔬 manage 청산결정을 _exit_decision()로 추출(테스트가능·우선순위 실적>손절>목표>RSI) [T7]. 검증:TestExitDecision 6케이스+라이브 manage 정상
36. ✅🔬 manage() tradable 게이팅 테스트(장닫힘→sell미호출·열림→호출) [T8]. 검증:TestManageGating 2케이스
37. ✅🔬 _days_to_earnings 경계 테스트(미래+·today0·과거-·미상None·malformed None)+block조건 [T9]. 검증:TestDaysToEarnings

## 야간 루프 배치 3 (02:4x KST)
38. ✅🔬 _realized_trades: 체결가 미상(≤0) 매도는 lot 소비/기록 안 함(오매칭·P&L오염 방지) [CX5]. 검증:test_unknown_price_sell_skipped
39. ✅🔬 _strategy_positions: oversell 시 qty 음수 방지(max 0 클램프) [CX17]. 검증:test_oversell_clamps_to_zero
40. ✅🔬 스크리너 존 게이트를 표시값(반올림 RSI)과 일치(경계 불일치 제거) [CX20]. 검증:컴파일+스크리너 실행

## 야간 루프 배치 4 (03:0x KST)
41. ✅🔬 --report 지표 확장: 기대값·최대낙폭·Sharpe(_report_stats 추출·테스트가능) [T18]. 검증:TestReportStats 2케이스
42. ✅🔬 manage _rsi 완료봉(c[:-1])으로 — 스크리너/진입과 일관·재현성(RSI≥65 익절 지터 방지) [CX16]. 검증:컴파일+라이브
43. ✅🔬 스크리너 c_done 경계 정확히 155봉(closes_all>=155 기준) [CX15]. 검증:스크리너 커버리지100% 정상

## 야간 루프 배치 5 (03:2x KST)
44. ✅🔬 _read_journal이 fallback.jsonl 병합+중복제거+시간순(critical 실패 체결 유령 방지) [MS9/CX8]. 검증:TestJournalMerge
45. ✅🔬 저널 파일 권한 0600(계좌 원장 world-readable 방지) [MS16]. 검증:컴파일+_j chmod
46. ✅🔬 _recent_trade 타임스탬프 견고화(Z/epoch/naive→KST). ⚠️`0<=age`가 시계오차 미래ts 거부하는 회귀 발생→라이브 --manage 🔴로 즉시 감지→-120초 허용치로 수정, 🟢 복구 [MS11/CX12]. 검증:TestRecentTrade 5케이스+라이브 🟢

## 야간 루프 배치 6 (03:4x KST)
47. ✅🔬 _entry_guard: 진입 직전 논지 독립 재검증(상승추세·RSI 38~58 매수존·비과열) — buy()에 배선, 과열 기대감주 매수차단(사용자 인사이트) [S14]. 검증:TestEntryGuard 5케이스
48. ✅🔬 진입 논지 방어심층(크론이 넘긴 심볼도 buy()가 재확인, 잘못된 심볼 맹목매수 방지) [S15]. 검증:TestEntryGuard+컴파일
49. ✅🔬 스크리너 추세깨짐(150일선 아래) 후보표서 제외 [SC20]. 검증:스크리너 실행(추세깨짐 미표시·커버리지100%)

## 야간 루프 배치 7 (04:0x KST)
50. ✅🔬 _in_cooldown: 손절 후 2일내 동일종목 재매수 차단(리벤지·휩쏘 방지, 익절은 허용) — buy()에 배선 [S6]. 검증:TestCooldown 3케이스
51. ✅🔬 스크리너 낙폭(falling-knife) 가드: 20일 -12%↓ 급락은 매수존 제외+15점 감점(RSI 낮은 이유가 붕괴인 것 배제) [SC7]. 검증:컴파일+스크리너 커버리지100%

## 야간 루프 배치 8 (04:2x KST)
52. ✅🔬 _open_symbols_and_deployed 부분매도 순qty·순usd 비례 반영(전체 슬롯 해제로 물타기·예산 가드 우회되던 HIGH 버그 수정) [MS3/CX2]. 검증:TestOpenAndDeployed 4케이스+라이브 --status(4종목 $76.9 정확)
53. ✅🔬 sell()을 전략 보유분(min(sellable,owned))으로 클램프+전략 미보유 종목 매도 거부(레거시/오입력 보호) [MS8/CX18]. 검증:컴파일+49테스트+클램프로직

## 야간 루프 배치 9 (04:4x KST)
54. ✅🔬 청산사유 분해(B5): _exit_category(EARN/STOP/TARGET/RSI)→sell reason_cat 인자→SELL 이벤트 exitCat 기록→_realized_trades 전파→--report 사유별 승률/손익. 어떤 청산룰이 돈 버는지 학습 [T19]. 검증:TestExitCategory 2케이스+라이브 --report/--manage
55. ✅🔬 intraday_entry_check day-change 게이트 테스트(급등 차단·조회실패 fail-closed) [T12]. 검증:TestIntradayGate 2케이스

## 야간 루프 배치 10 (05:0x KST)
56. ✅🔬 서킷 ET 거래일 버킷(_trading_day): KST 자정이 US 정규장 중간이라 KST date로 세면 장중 진입/손절 상한이 리셋되던 HIGH 버그 수정 [CX3]. 검증:TestTradingDay(KST자정 넘어도 같은 ET세션)+TestCircuit
57. ✅🔬 _days_to_earnings도 ET 거래일 기준(실적 청산 off-by-one 방지) [CX11]. 검증:TestDaysToEarnings ET기준+라이브 GOOGL 실적 8d

## 야간 루프 배치 11 (05:2x KST)
58. ✅🔬 추세이탈 청산(C7/S5): cur<150일선이면 -8% 기다리지 않고 청산(진입논지 무효). _exit_decision에 ma150 파라미터+_ma150 헬퍼. 우선순위 손절>추세이탈>목표. 검증:TestExitDecision 2케이스+라이브(현 포지션 미발동)
59. ✅🔬 시간청산(C6/S4): MAX_HOLD_DAYS(20)넘게 보유 & 진전<3%면 죽은자금 회수. _exit_decision days_held+_days_held 헬퍼. 검증:TestExitDecision 2케이스(진전시 미발동)

## 야간 루프 배치 12 (05:4x KST)
60. ✅🔬 포트폴리오 실현손실 서킷(C9): 오늘 실현손실 ≤ -$8이면 손실청산 횟수와 무관하게 당일 신규 중단(자본보호·2번 정상손절≠1번 갭참사) [S9]. 검증:test_dollar_loss_circuit
61. ✅🔬 스크리너 실적임박 점수화(D5): 기대감 윈도우(5~12일) +6점 가점·발표임박(0~2일) 매수존 제외. GOOGL/XOM/LLY 기대감주 상위로 [SC8]. 검증:스크리너 GOOGL 39.6점 상승·커버리지100%

## 야간 루프 배치 13 (06:0x KST)
62. ✅🔬 변동성 반영 손절(C12/S12): 고정 -8% 대신 -max(8, 1.5×ATR%) — 저변동은 -8 유지, 고변동은 넓혀 노이즈 손절 방지. _exit_decision atr_pct+_atr_pct 헬퍼. 검증:TestExitDecision 2케이스(widen/floor)+라이브
63. ✅🔬 스크리너 신선도 검출(D10/SC13): 최신봉 5일↑ 오래되면(데이터갭/거래정지/상폐) 제외 — stale가를 현재가로 오인 방지. 검증:커버리지100%(정상데이터 미제외)

## 야간 루프 배치 14 (06:2x KST)
64. ✅🔬 _day_change 부분봉 판정(CX14): 최신봉이 진행중(종가≈현재가)이면 직전봉 기준, 완료봉(오늘봉 없음)이면 최신봉 기준 — 프리장 개장직전 2일치 측정 오류 방지. 검증:TestDayChange 2케이스
65. ✅🔬 스크리너 상대강도(vs QQQ 3개월, ±8캡 SC5): 지수 대비 초과수익 가점 — 시장 리더 우대·라거드 감점. 검증:스크리너 TXN(+61%) 상승 반영

## 야간 루프 배치 15 (06:4x KST)
66. ✅🔬 브레이크이븐 청산(C4/S2): 최고 +4%(MFE) 갔던 종목이 본전 이하로 오면 청산 — 이익 반납 방지. _exit_decision mfe_pct+_mfe_pct 헬퍼(일봉 high 기준). 우선순위 손절>추세이탈>브레이크이븐>목표. 검증:TestExitDecision 3케이스
67. ✅🔬 저널 이벤트에 clientOrderId(coid) 스탬프(E17/MS17): 서버 감사로그·브로커 주문과 사후 조인 가능. buy·sell SELL/BUY_FILLED. 검증:컴파일+66테스트

## 야간 루프 배치 16 (07:0x KST)
68. ✅🔬 매수 직전 USD 매수가능 재확인(E18/MS18 backstop): plan-후-체결 사이 FX/타 체결로 자금 소진 시 로컬 fail-closed(서버 거부 전에). 검증:컴파일+66테스트
69. ✅🔬 1분봉 3분 모멘텀을 완료봉 기준으로(CX21): 진행중 분봉 제외 → 추격게이트 오탐 감소·안정. 검증:라이브 진입게이트 정상

## 야간 루프 배치 17 (07:2x KST)
70. ✅🔬 스크리너 갭 처리(D15/SC10): 당일 |갭|>3%(오늘시가 vs 어제종가)면 매수존 제외+8점 감점 — 갭 뜬 종목은 눌림 아님. 검증:스크리너 정상
71. ✅🔬 스크리너 볼륨확인(D7/SC2): 눌림이 저볼륨(20일평균<0.9)이면 가점·대량매도(>1.5)면 감점 — 건강한 눌림 vs 분산 구분. 검증:GOOGL 41.7점(저볼륨 눌림 가점)

## 야간 루프 배치 18 (07:4x KST)
72. ✅🔬 포트폴리오 미실현 손익 요약+손실경보(B7/T22): manage 매 사이클 평가/원가/미실현% 출력, -5%↓면 ⚠️경보+PORTFOLIO_ALERT 저널(단 HALT 안 함=손절은 계속 발화). 검증:라이브 "평가$77.39/원가$76.90 +0.6%"
73. ✅🔬 orderId 없는 정상응답 → HALT(E15/MS15): 409 idempotent/빈응답 시 phantom working 대신 동결+수동확인. buy 배선. 검증:컴파일+66테스트

## 2라운드 재감사 (17:1x KST) — 진화된 코드 새 버그
74. ✅🔬 _open_symbols_and_deployed BUY_WORKING→BUY_FILLED 배치액 이중계산 버그 수정(fusd/wusd 분리, 확정은 fusd만) — 어젯밤 E3서 넣은 잠복버그, 재감사서 발견 [A13]. 검증:test_working_then_filled_no_double_count+라이브 $76.9
75. ✅🔬 스크리너 실적일 ET 통일(SC1): execute와 동일 ET 거래일 → KST 하루 어긋남 제거. 검증:스크리너 정상
76. ✅🔬 스크리너 실적가점 6→3(SC4): 안정전략에 과한 가점 완화(발표 앞둔 갭 위험주가 깨끗한 눌림 압도 방지). 검증:GOOGL 41.2
77. ✅🔬 스크리너 갭 fail-closed(SC10): 갭 계산 실패시 gapped=True로 안전 제외. 검증:컴파일
78. ✅🔬 스크리너 명시적 None체크(SC11): rsi/ma 0.0을 결측 오인 방지. 검증:커버리지100%
79. ✅🔬 _entry_guard를 zone 직접확인으로(#14): knife/gap/실적임박까지 재검증(기존엔 up/rsi/ext만) — 더 완전한 방어심층. 검증:TestEntryGuard+라이브 XOM
80. ✅🔬 sell DUPLICATE-adopt에 exitCat 전달(#13): 중복채택 매도도 청산사유 분해에 잡힘. 검증:컴파일
81. ✅🔬 변동성손절 라벨 정확화(#21): 실제 넓어졌을 때(1.5ATR>8)만 '변동성' 표기. 검증:67테스트
82. ✅🔬 _exit_category 분류 세분화(TREND/BREAKEVEN/TIME 추가): --report 청산사유 학습이 추세이탈·브레이크이븐·시간청산을 구분(기존 OTHER 뭉침 해소) [A4]. 검증:test_new_categories
83. ✅🔬 _report_stats sharpe·PF=inf·1건 sd=0 테스트 [A14]. 검증:TestReportStats 3케이스
84. ✅🔬 _mfe_pct 진입후 최고상승폭 테스트(진입전 제외·None·전부이전) [A10]. 검증:TestMfePct 3케이스
85. ✅🔬 시간청산 캘린더일 보정(exec#17): MAX_HOLD_DAYS 20→28(≈20거래일) — _days_held가 주말포함이라 1주 조기청산 방지. 검증:test_time_exit(days30) 갱신
86. ✅🔬 check_journal merged view(exec#19/MS16): fallback에만 남은 체결도 무결성 검사에 포함(기존 main만). 검증:TestCheckJournal 3케이스+라이브 merged 19건
87. ✅🔬 스크리너 RS 비활성 명시경고(SC6/18): QQQ 조회실패로 상대강도 꺼질 때 조용히 안 넘어가고 출력. 검증:컴파일
88. ✅🔬 레짐 게이트(RB5/S7): QQQ 200일선 아래(하락장)면 신규 진입 억제(index_engine 200MA 재사용, 판단불가는 진입허용) — 하락장 눌림매수 회피. buy() 배선. 검증:TestRegime 3케이스+라이브(현재 risk_on)
89. ✅🔬 sell 이중클램프+미보유거부 테스트(TA5): 1.0→전략보유0.5→sellable0.3 클램프·전략미보유는 plan 전 거부. 검증:TestSellClamp 2케이스
90. ✅🔬 포트폴리오 요약 매도분 제외(SL4/exec#18): 이번 사이클 청산되는 포지션은 미실현 요약·경보서 제외(보유분만 집계). 검증:라이브 4보유 정상
91. ✅🔬 _j 완전실패시 하드종료(SL11/MS12): main+fallback 둘 다 실패하면 무기록 매매 금지 위해 sys.exit(3). 검증:컴파일+정상 _j 유지
92. ✅🔬 buy 가드순서 테스트(TA7): 금액미달→cooldown/circuit 전 차단·물타기→circuit/earnings 전 차단. 검증:TestBuyGuardOrder 2케이스
93. ✅🔬 스크리너 실적 4일내 매수존 제외(SL15/SC5): 기존 2일→4일(3~4일 갭위험도 배제). 검증:스크리너 커버리지100%
94. ✅🔬 1분봉<5 fail-closed(SL12/exec#15): 개장직후·희소데이터에 통과 대신 안전대기(추격게이트 우회 방지). 검증:test_sparse_1m_fail_closed
95. ✅🔬 metrics.jsonl 에쿼티 시계열 스냅샷(N6): manage 매 사이클 배치/미실현/포지션수 기록 → 추세 모니터링·차트용. 검증:라이브 metrics 기록됨
96. ✅🔬 시세실패해도 실적청산 확인(SL2/exec#16): manage가 get_price 실패 시 스킵 대신 실적임박(가격불필요) 청산은 수행 → 실적 전날 시세오류로 청산 놓침 방지. 검증:컴파일+라이브
97. ✅🔬 _market_open 캘린더 백스톱 테스트(TA15/A15): 휴장오작동+실체결→개장·휴장+무체결→휴장·정상캘린더→개장. 검증:TestMarketOpen 3케이스
98. ✅🔬 heartbeat last_run(N3): manage 매 사이클 실행시각 기록 → 죽은 크론/멈춘 루프 감지 기반. 검증:라이브 last_run 기록됨
99. ✅🔬 --verify-broker(N1): 전략 저널 원장 vs 브로커 실보유 대조, 원장>브로커(유령롱) 드리프트시 exit1. 검증:라이브 4종목 원장=브로커 완전일치 확인(레거시 제외)
100. ✅🔬 _reconcile_fill PARTIAL_FILLED 비terminal 테스트(TA9): 부분체결은 break 안 하고 재시도 다 소진(현 동작 문서화, MH2 사전핀). 검증:TestReconcilePartial
101. ✅🔬 sell 레거시 최우선 거부 테스트(TA6): AAPL 등 레거시는 plan 전 차단. 검증:test_legacy_refused
102. ✅🔬 피크대비 낙폭 서킷(RB7/C16/S10): 실현 에쿼티 피크대비 -15%(자본기준) 낙폭이면 신규 진입 중단 — 연속손실 보호(일일 서킷 위 계좌레벨). 검증:test_peak_drawdown_circuit
103. ✅🔬 manage 포트폴리오 손실경보 테스트(TA1): -6% 포트폴리오→PORTFOLIO_ALERT 발생 확인. 검증:TestManagePortfolio
104. ✅🔬 _read_journal fallback 단독+malformed/blank skip 테스트(TA11): main없고 fallback만 있어도 유효분 복원·깨진줄 스킵. 검증:TestReadJournalFallback
105. ✅🔬 us_session ET-aware(RB4/CX4): zoneinfo로 EDT/EST 자동+주말 휴장 처리(기존 고정 KST는 겨울 EST에 1시간 어긋나고 주말 미체크). 검증:TestUsSession 4케이스+라이브 프리마켓 정확
106. ✅🔬 스크리너 유동성 하한(RB10/SC1): 20일 중앙 거래대금 $20M 미만 제외 — 얇은 종목 슬리피지 함정 방지. 검증:커버리지100%(대형주 전부 통과)
107. ✅🔬 sell reconcile-불가시 HALT(SL1/exec#10): 매도 응답 orderId없음 or 체결확인 전부실패(st=None)면 SELL_WORKING 조용히 넘기지 않고 동결+수동확인(buy E15와 대칭, 매도 유령 방지). 검증:컴파일+94테스트+라이브
108. ✅🔬 스크리너 지수백오프 재시도(RB12/SC12): 고정 1s×3 → 1→2→4s 백오프(레이트리밋/일시장애 상관실패로 커버리지 붕괴 완화). 검증:컴파일+스크리너
109. ✅🔬📚 [실전 재학습] 추세이탈 청산에 2% 버퍼 추가: MDLZ가 150일선 0.3%(노이즈) 하회로 성급히 청산된 것 학습 → TREND_BREAK_BUFFER=0.02, 결정적 하회(2%↓)에만 발동. 검증:test_trend_break_needs_buffer+MDLZ값 재현시 보유
110. ✅🔬 브레이크이븐 MFE도 완료봉 '종가'+ET진입일(SL9/exec#4·6): _mfe_pct가 일봉 high(wick)·문자열 date로 계산 → 진행중 부분봉/꼬리 급등에 브레이크이븐 오발동 위험. close 기준·c[:-1]로 진행중봉 제외·_trading_day(ET) 비교로 교정(MDLZ 노이즈청산 학습과 동일 원리). 검증:test_since_entry_completed_close/excludes_in_progress_bar/all_before_entry+97테스트
111. ✅🔬 manage() 다종목·다규칙 end-to-end 테스트(TA3): 한 사이클서 A(-10%→STOP)/B(RSI70→RSI)/C(보유)가 종목별로 정확한 룰 발동+reason_cat 전달 검증(회귀시 규칙 교차오염 즉시 탐지). 검증:test_each_rule_fires_correctly+97테스트
112. ✅🔬 TA2 manage 시세실패 중립회계 테스트: get_price 예외시 매도 안 하고 원가=평가(중립 0%)로 계상됨을 stdout 캡처로 검증. 검증:test_price_fail_neutral_no_sell
113. ✅🔬 TA16 --status 종료코드 순수함수 분리+매트릭스: HALT·미병합fallback·손실서킷만 exit1, 진입상한은 정상0. 검증:_status_exit_code+test_matrix
114. ✅🔬 SL8 sell 중복 개장체크 제거: _guard(skip_market=True)로 manage 경로는 세션·캘린더 재조회 생략(HALT·실주문 가드는 유지). 검증:test_skip_bypasses_session_calendar/test_skip_still_requires_live_orders
115. ✅🔬 SL6 추세이탈 attribution 정확화: 수익(pl>0)+목표/RSI 도달이면 라벨을 TARGET/RSI로(청산 여부 동일, 학습분해만 정확). 검증:test_trend_break_profit_at_target_labels_target/_rsi/_loss_still_trend
116. ✅🔬 SL14 스크리너 부분봉 판정: 오늘(ET)==최신봉일 때만 c[:-1] — 장 마감시 완료 전일봉 유지(신선도 손실 방지). 검증:_partial_drop+3테스트
117. ✅🔬 RB6 미상실적 갭가드: 실적일 미상(None)+당일 |변동|≥4% → 미상 실적이벤트 의심 진입차단(fail-closed, 실적아는 종목은 EARN_BLOCK_DAYS가 처리). 검증:_earn_unknown_gap_block+4테스트
118. ✅🔬 RB11 _fetch_daily 페이지네이션: 첫 호출 부족시 before 커서로 과거 봉 추가해 ≥252봉 확보(MA150 조용한 결측 방지). 검증:test_single_call_enough/paginates_when_short/empty
119. ✅🔬 SL7 manage 지표 fetch 통합: 종목당 _fetch_daily 1회로 rsi/ma150/atr/mfe 공유(~5콜→1콜, 레이트리밋 완화·일관성). 헬퍼에 candles= 옵션(하위호환). 검증:test_single_fetch_shared+기존 manage테스트
120. ✅🔬 RB8 pre-earnings 마지막세션 청산 보장: 캘린더 dte만 쓰면 금요일(월요일 실적)=dte3이라 청산 누락 → 주말/휴일 건너뛴 '마지막 거래세션' 판정으로 dte>1이어도 청산(실적 관통 방지). 검증:_is_last_session_before_earn+_exit_decision 5테스트
121. ✅🔬 RB14 갭관통 손절 구분+경보: 손절선을 마진(2%) 이상 관통한 급락 갭을 정상손절과 구분(GAPSTOP 카테고리)+GAP_THROUGH_STOP 저널 경보. ATR로 넓힌 스탑에도 상대판정 견고. 검증:test_gap_through_distinct_from_stop/relative_to_widened_stop
122. ✅🔬 RB15 --reconcile 명령(READ-ONLY): 미해결 working/uncertain 주문을 브로커에 재조회해 현재상태만 보고(저널 미기록 — 체결반영은 MH 수동, 이중기록 방지). 검증:_pending_working_orders 4테스트+라이브스모크
123. ✅🔬 RB13 구조적 로깅 execute.log: _log로 실행 START/END+rc를 타임스탬프와 함께 append(죽은 크론/실패 사후추적). __main__ 디스패치 정리(동작 보존). 검증:test_log_appends+라이브(--status가 START/END 기록)
124. ✅🔬 N5 --selftest 프리플라이트 게이트: 설정/클라이언트/저널파싱/실적·목표파일/HALT부재/fallback병합/락 점검, 치명실패면 exit1(크론 게이트용, 주문 없음). 검증:_selftest_gate 3테스트+라이브 올그린
125. ✅🔬 N4 크리티컬 이벤트 out-of-band 알림: _alert(지속 alerts.jsonl+운영로그+opt-in macOS알림), _j에서 alert-worthy 이벤트(WORKING/PORTFOLIO_ALERT/GAP_THROUGH/CIRCUIT) 중앙 발화(주문로직 무손상). 검증:test_alert_writes+test_j_fires_alert 2테스트
126. ✅🔬 N2 --backtest-strategy(청산룰 what-if READ-ONLY): _backtest_exit가 진입후 일봉을 하루씩 진행하며 현 _exit_decision 적용→언제 어떤 룰로 나갔을지 시뮬(룰 튜닝 학습, 주문 없음). 검증:_backtest_exit 4테스트+라이브(4종목)
127. ✅🔬 SL10 전략 하드백스톱(config KRW 대신 안전선택): 예산/저널과 독립된 HARD_MAX_ORDER_USD(65)/HARD_MAX_DAILY_USD(82) 2차 상한을 execute.py에만 적용(config KRW 전역 낮추면 수동/MCP 주문까지 캡돼 위험 → 회피). 검증:_today_deployed_usd+하드상한 스케일 2테스트
128. ✅🔬🚨 [회귀수정] _fetch_daily count>200 → API 400: 토스 캔들 API는 count≤200만 허용인데 RB11이 252 요청 → 첫 호출부터 400 → 빈 리스트 → RSI/MA150/ATR/MFE 전부 결측 → RSI익절·추세이탈 청산이 조용히 꺼짐(가격기반 손절/목표/실적은 정상 유지). 페이지당 count 200 캡+호출부 200으로 수정, 라이브 RSI/MA150 복구 확인. 검증:test_page_count_capped_at_200+139테스트+라이브 --manage RSI 표시
129. ✅🔬 [재분석] 단일종목 과집중 가드: NVDA가 배치의 45.5%를 차지(고변동주가 포트폴리오 리스크 지배) 발견 → MAX_POSITION_WEIGHT=0.40, 단일종목 예산 40%($31) 초과 진입 차단(_concentration_ok). 기존 NVDA는 자르지 않고 목표도달 시 자연축소, 재발만 방지. 검증:test_within_cap/over_cap_blocked/small_ok+테스트 전체
130. ✅🔬 T7/K5 HALT 일원화: 8곳의 무언 touch() → _set_halt(사유를 HALT 파일에 기록+out-of-band 경보). 왜 동결됐는지 파일만 봐도 파악. 검증:163테스트+HALT부재 라이브
131. ✅🔬 T5/MH7 체결기록 멱등화: _record_fill이 orderId별 기록된 수량 차감 후 '증분만' 저널 — 재기록·DUPLICATE채택·재폴링 겹침에도 이중계상 불가. 검증:test_same_order_twice_no_double
132. ✅🔬 T3/MH2 부분체결을 부분으로: PARTIAL_FILLED → BUY_PARTIAL/SELL_PARTIAL(cum 누적필드) — 전량체결로 오기록하던 것 교정, 잔량 추적 가능. 검증:test_partial_then_more_records_delta/sell_partial_named
133. ✅🔬 T2/MH1b 평균가 폴백: _reconcile_fill이 averageFilledPrice 미상 시 filledAmount/수량 역산 + SELL 이벤트에도 filledUsd 기록. 검증:컴파일+통합테스트
134. ✅🔬 T6/MH6 죽은주문 DUPLICATE 채택 차단: 기존주문이 CANCELED/REJECTED(체결0)면 buy=BUY_REJECTED(유령슬롯 방지)·sell=SELL_RETRY(손절 삼킴 방지, 다음 사이클 재매도). 검증:test_buy_dup_dead_order_released/test_sell_dup_dead_order_retries
135. ✅🔬 T7/MH4 buy 체결확인 전부실패 → BUY_UNCERTAIN+HALT: '미체결' 단정 금지(체결됐을 수 있음), sell SL1과 대칭. 검증:test_buy_reconcile_none_halts
136. ✅🔬 T8/MH3 stale-coid 이중롱 회귀고정: dup채택도 _record_fill 멱등 경유 → 같은 주문 두 번 채택돼도 증분 0. 검증:멱등 테스트 3종
137. ✅🔬 T4/MH5 미확정주문 재폴링: manage 시작마다 working/uncertain/partial 재조회 → 체결 증분 승격·죽은주문 슬롯해제·부분체결 잔량종결(BUY/SELL_REMAINDER_CANCELED). 검증:TestRepollPending 4종+라이브 manage
138. ✅🔬 T1/MH1 체결가미상 역산: _realized_trades가 fp/sp≤0이면 filledUsd/수량으로 복원 — lot 유실·P&L 누락·쿨다운/서킷 오염 방지. 검증:TestRealizedPriceDerivation 3종
139. ✅🔬🐛 [테스트가 잡은 실버그] BUY_WORKING→BUY_REJECTED 후에도 슬롯 점유: _open_symbols_and_deployed가 REJECTED로 working 플래그 안 지움 → 재폴링 해제가 무효였음. 수정+검증:test_dead_working_released
140. ✅🔬 T9/MH8 MCP 주문경로 락 게이트: execute.py 실행중(락 보유) MCP place_order_confirmed 거부(교차 이중매수/coid충돌 방지), execute 자신은 env 표식으로 통과. 검증:라이브 3케이스(없음/외부/자신)
141. ✅🔬 T10 verify-broker 역드리프트: 브로커>원장(미기록 수동/MCP 매수 의심) 경고 — MH8 감시 루프 완성(전략은 원장분만 매도라 안전, 가시화만). 검증:컴파일+라이브
142. ✅🔬 T11 설정파싱 실패 가시화: targets/earnings JSON 깨지면 silent {} 대신 1회 경고+CONFIG 경보(익절출구·실적가드 조용한 소실 방지). 검증:test_targets_parse_warn
143. ✅🔬 T12 PORTFOLIO_ALERT ET일 1회 dedup: 30분마다 중복 경보 스팸 제거(상태파일). 검증:test_portfolio_alert_below_5pct_once_per_day(2회 호출 1회 경보)
144. ✅🔬 T13 로그 로테이션: execute.log/alerts.jsonl/metrics.jsonl 5MB 초과 시 .1 회전(원장 저널은 제외). 검증:test_rotate
145. ✅🔬 T14 장부 일일백업: 뮤테이팅 실행 전 .backups/journal-ET일.jsonl 1회 복사·14일 보관. 검증:test_backup_journal+라이브 생성 확인
146. ✅🔬 T15 _day_change ET날짜 판정: 0.1% 가격일치 휴리스틱 → 최신봉 날짜==ET오늘(스크리너 SL14와 일관). 검증:test_in_progress_bar_uses_prior 갱신
147. ✅🔬 T16 --status 백스톱 뷰: 오늘투입/하드일일한도·단일주문·비중상한·최대비중 종목 표시. 검증:라이브(NVDA 45% 표시)
148. ✅🔬 T17 --selftest 확장: targets/earnings 파싱 자체검증+보유종목 목표가 커버리지(TARGET룰=돈줄 누락 조기경보). 검증:라이브 11체크 올그린
149. ✅🔬 T18+T19/20 serenity 신선도 경고(아카이브 4일↑ 미갱신 시)+머니경로 테스트 배터리 21종 추가(총 163개). 검증:163테스트 전체 통과
150. ✅🔬 A10 _reconcile_fill 지수백오프: 고정 1.5s → 1→2→4s(상한8s) — 레이트리밋/일시장애 상관실패 완화. 검증:178테스트
151. ✅🔬 A12 --merge-fallback: critical 기록실패로 fallback에만 남은 체결을 메인 병합(멱등·중복스킵), 원본은 .merged-ts로 보존(감사 가능). 검증:test_merge_dedup_and_preserve+라이브
152. ✅🔬 B3/K6 kill-switch 통합뷰: --status에 env TOSS_KILL 활성 여부 표시(HALT와 별개 채널 가시화). 검증:라이브
153. ✅🔬 B4 MCP 주문 알림: 비-execute 경로 주문 성공 시 alerts.jsonl 기록(조용한 수동주문 가시화 — 역드리프트 감시와 짝). 검증:컴파일+게이트 로직
154. ✅🔬 C4 시간청산 거래일 기준: _trading_days_held(주말 제외)로 캘린더 28일≈20거래일 근사 대체(연휴 낀 조기/지연 청산 오차 제거). MAX_HOLD_DAYS=20거래일. 검증:TestTradingDaysHeld 3종
155. ✅🔬 C7 섹터 중복 소프트 가드: SECTORS 맵+_sector_overlap — 같은 섹터 진입 시 경고+저널(차단 아님, 분산 판단 보조). 검증:TestSectorOverlap 3종
156. ✅🔬 D1 스크리너 실적일 컬럼: earnings_dates.json 통합 표시(GOOGL 6d 등) — 표에서 실적임박 즉시 확인. 검증:라이브 출력
157. ✅🔬 D8 선별 스냅샷: screener_last.json에 상위표(점수·RSI·존) 저장(재현성·사후검증). 검증:test_diff_and_snapshot
158. ✅🔬 D10 사이클 diff: 직전 매수존 대비 신규진입/이탈 자동 표시(셋업 변화 놓침 방지). 검증:test_diff_and_snapshot+라이브
159. ✅🔬 E4(+E3) serenity --digest: 마지막 다이제스트 이후 새 트윗만 요약+신규 티커 집계+보유/워치 언급 하이라이트. 검증:라이브(첫 실행 기준선 저장)
160. ✅🔬 F5 --report 최근 7일 창: 전체 누적과 별도로 주간 승률·손익(학습 리뷰용). 검증:컴파일+라이브
161. ✅🔬 F6 --equity: metrics.jsonl 스냅샷 ASCII 곡선+실현누적(추세 한눈에, READ-ONLY). 검증:라이브(40스냅샷)
162. ✅🔬 F8 last_run 노화 감시: --status에 경과분 표시, 정규장인데 2h↑면 관리크론 정지 의심 경고. 검증:라이브(0분 전)
163. ✅🔬 G3 _atomic_write: 상태파일(last_run/alert_day) tmp→rename 원자적 쓰기(크래시 반쪽저장 방지). 검증:test_atomic_write
164. ✅🔬 G9 데이터파일 0600: 원자적쓰기·백업 경로 소유자 전용 퍼미션. 검증:test_atomic_write 경유
165. ✅🔬 G10 백업 체크섬: 일일백업에 sha256 동반 기록+--selftest서 최신 백업 무결성 검증(복구선 자체 손상 감지). 검증:test_backup_checksum_roundtrip(훼손→탐지)
166. ✅🔬 G5 --scan-journal: 저널/fallback 손상라인 위치 리포트(READ-ONLY, 원본 무수정 — 백업과 대조용). 검증:라이브(손상 없음)
167. ✅🔬 H4 서킷 경계값 매트릭스: 진입상한-1 허용/도달 차단·손실수·$손실 임계 테스트(ET tz 명시). 검증:TestCircuitBoundaries 3종
168. ✅🔬 H8 서버 락게이트 테스트: 락없음 통과/락경합 차단/execute 자신 통과 3케이스 회귀 고정. 검증:test_gate_matrix
169. ✅🔬 I2 서킷 저널 이중파싱 제거: _realized_trades 1회 계산 재사용(사이클당 파싱 절반). 검증:178테스트
170. ✅🔬 C1 트레일링 이익보전: MFE≥6% 갔던 종목이 최고이익 절반 밑으로 반납(0<pl≤mfe×0.5)하면 청산(TRAIL) — '기대감/강세에 판다' 코드화, 완료봉 close MFE(MDLZ 학습 일관). 검증:TestTrailAndRsiRules 4종
171. ✅🔬 C2 부분익절: +5% 도달 시 절반 익절(PTP)·잔량 러너 — 이익 잠금+상방 유지. _partial_tp_taken이 '현재 포지션' 기준 판정(종결 시 리셋, 이월 방지). 검증:TestPartialTp 5종(manage e2e 포함)
172. ✅🔬 C3 RSI 익절 이익조건: RSI≥65여도 손실이면 미발동(익절은 이익 실현일 때만 — 손실 중 RSI 청산 비합리 제거). 검증:test_rsi_exit_requires_profit
173. ✅🔬 C5 --suggest-targets(READ-ONLY): 진입가×(1+max(4%,1.5×ATR)) 제안 vs 현재 목표 비교 출력(자동 변경 없음). 라이브: 4종목 전부 '유지 권장'(수동 목표 적정 확인). 검증:라이브
174. ✅🔬 C8 실적 직후 재진입 차단: dte -1~2 차단(발표 직후 IV크러시/갭 소화기 1일). 검증:TestPostEarnBlock 경계
175. ✅🔬 D2 스크리너 섹터 태그: 매수존 요약에 섹터 병기(execute.SECTORS 단일진실 재사용) — 분산 판단 즉시 가능. 검증:라이브(GE(산업), GOOGL(테크)…)
176. ✅🔬 D3 유동성 하한 $20M→$50M: 유니버스가 메가캡이라 실질 영향 0(커버리지 100% 유지), 안전마진만 상향. 검증:라이브 커버리지
177. ✅🔬 E2 serenity 톤 집계: 최근 언급 20개 키워드 기반 강세/약세 근사(참고용 명시). 라이브: NVDA 강세7/약세10 중립. 검증:라이브
178. ✅🔬 E5 serenity 로컬 미러: 트윗 아카이브 다운로드 성공 시 gzip 일일 스냅샷(7일 보관) — 원 저장소 소실 대비. 검증:컴파일+fetch 경로
179. ✅🔬 G4 --set-target/--set-earnings: 검증(숫자/ISO날짜)+원자적 저장 — 손편집 JSON 파손으로 익절출구/실적가드 소실 방지. 검증:TestSetCommands 2종
180. ✅🔬 G6 metrics 스키마 버전(v:1): 향후 포맷 변경 시 하위호환 판별. 검증:컴파일
181. ✅🔬 G7 tz 폴백 월기반: zoneinfo 부재 시 고정 EDT(-13h) → 3~11월 13h/12~2월 14h(겨울 하루 어긋남 제거), 스크리너 동일 적용. 검증:TestKstEtOffset
182. ✅🔬 K1 토큰 at-rest 퍼미션 selftest: token.json 존재 시 0600 검증, 미저장(in-memory)이면 안전 표기. 라이브: 미저장 확인. 검증:라이브
183. ✅🔬 K2 키 로테이션 리마인더: 토큰 파일 90일↑ 경과 시 selftest 경고. 검증:selftest 경로
184. ✅🔬 H3 sell 클램프 조합 테스트: 요청→보유분→가능수량 이중 클램프 + 가능수량 조회실패 시 보유분만. 검증:TestSellClampCombos 2종
185. ✅🔬 H5 manage 부분체결(부분매도) e2e: manage가 +5%서 PTP 절반매도 호출·이미 실행 시 중복 방지(러너 유지). 검증:TestPartialTp manage 2종
186. ✅🔬 H9 골든 저널 회귀: 대표 이벤트 믹스 픽스처(working→filled·partial 증분·가격역산·PTP·REJECTED 슬롯해제)의 포지션/실현손익/무결성 스냅샷 고정. 검증:TestGoldenJournal 3종
187. ✅🔬 H10 ci.sh: 원커맨드 컴파일+전체테스트(개선 후 이것만 실행). 검증:라이브 통과
188. ✅🔬 I5 manage 심볼별 예외 격리: 한 종목 예외(API/파싱)가 전체 관리를 죽이지 않게 try/except+중립 회계. 검증:197테스트+라이브
189. ✅🔬 J5 --holdings 미기록 태그: 전략/레거시 외 보유에 '[미기록 — 수동/MCP]' 표시(역드리프트 가시화 보완). 검증:컴파일
190. ✅🔬 A9 매도 연속실패 경보: 오늘(ET) SELL_FAIL/RETRY 3연속이면 manage서 경보(주문경로 점검 신호) — 손절 시도는 계속(차단 아님). 검증:test_sell_fail_streak(체결 시 리셋 포함)
191. ✅🔬 B2 --audit-join: 서버 감사로그↔전략저널 orderId 대사(READ-ONLY) — 저널 밖 주문(MCP/수동)과 감사 밖 체결(경로우회) 탐지. 라이브: 저널 체결 100% 감사 존재 확인. 검증:라이브
192. ✅🔬 B5 서버 일일 주문예산 표시: --status에 daily.json 기반 ₩사용/한도·건수(오늘 ₩0/1,000,000·0/10). 검증:라이브
193. ✅🔬 C6 레짐 상태 표시: --status에 risk_on/off(QQQ 200MA) — off여도 보유관리·손절·목표는 정상(신규만 억제) 명시. 검증:라이브(risk_on)
194. ✅🔬 C9 배당락 인지: dividend_dates.json 기반 manage에 '배당락Nd' 정보 표시(락일 하락=배당분 노이즈 인지, 액션 없음). 검증:test_days_to_dividend
195. ✅🔬 C10 쿨다운 정밀화: 손실 ≤-3%만 쿨다운(MDLZ류 -0.9% 노이즈컷은 미적용) — 좋은 셋업이 미세컷에 2일 잠기는 기회비용 제거(MDLZ 학습 연장). 검증:TestCooldownThreshold 3종(경계 -3% 포함)
196. ✅🔬 D4 RS 다중기간: 3개월 70%+1개월 30% 합성(결측 시 가중 재정규화) — 장기추세에 최근 자금흐름 가미. 검증:test_rs_blend 3케이스
197. ✅🔬 D7 스크리너 캔들 디스크캐시: TTL 20분(~/.toss-trader/cache/candles) — 30분 사이클 간 API 절감·레이트리밋 완화, 원자적 쓰기. 검증:컴파일+라이브
198. ✅🔬 E3 보유종목 Serenity 언급 manage 연동: 48h 내 언급을 캐시 파일만 읽어(무네트워크) 포트폴리오 요약에 ℹ️ 표시. 검증:컴파일+헬퍼 격리
199. ✅🔬 F7 알림채널 정책 표시: --status에 데스크톱 ON/OFF(env)·alerts.jsonl 상시 기록 명시. 검증:라이브
200. ✅🔬 H6 FIFO property 테스트: 시드 랜덤 20시퀀스 불변식(실현≤매수·잔여≥0·잔여≤매수-실현·P&L 정의 일관) — 오버셀 클램프 특성까지 문서화. 검증:test_random_sequences
201. ✅🔬 I1 _read_journal (mtime_ns,size) 캐시: 사이클당 수십회 저널 재파싱 제거, append/외부수정 시 자연 무효화. 검증:test_journal_cache_invalidates_on_append
202. ✅🔬 J2 report 원화 병기: 실현손익 ≈₩ 환산(실시간 환율, 조회실패 시 생략). 라이브: $+2.18≈₩+3,264@1,497. 검증:라이브
203. ✅🔬 J3 수수료 추정 순P&L: 편도 TOSS_COMMISSION_PCT(기본 0.1%) 기반 왕복 수수료 차감 표시(추정 명시). 라이브: -$0.13→순 +$2.05. 검증:라이브
204. ✅🔬 J6 목표 진행률 미니바: manage 라인에 [▓▓░░░] 5칸 바(XOM 46% 등 한눈에). 검증:라이브
205. ✅🔬 K3 config 한도 sanity(selftest): 주문/일일 한도·예산·종목수 0/음수면 치명 실패(가드 무력화 조기 발견). 검증:라이브
206. ✅🔬 K4 env 오버라이드 감사(selftest): 설정된 TOSS_* 나열(SECRET/TOKEN류 값 마스킹). 검증:라이브
207. ✅🔬 K8 로그 민감정보 스크럽: execute.log/alerts.jsonl 저장 전 Bearer·appkey/secret류 마스킹(원장은 대상 아님). 검증:test_scrub_masks_secrets(일반 텍스트 무변형 포함)
208. ✅🔬 K9 requirements 버전 고정: mcp==1.27.2 등 라이브 검증 조합 명시(무단 업그레이드로 주문경로 파손 방지, ci.sh 게이트). 검증:파일
209. ✅🔬 K10(+G8) RECOVERY.md: HALT 해제 절차·저널 복구(백업+체크섬)·fallback 병합·디스크풀·크론 사망·키 로테이션·완전 재설치 — 원장이 진실·검증 없는 해제 금지 원칙. 검증:문서+절차의 명령 전부 실존
210. ✅🔬 A11 저널 fsync: critical 체결 이벤트만 flush+fsync(전원단절에도 체결기록 보존 — 머니 내구성 마지막 조각, 일반 이벤트는 오버헤드 회피). 검증:207테스트+컴파일
211. ✅🔬 D9 실용판 — 스크리너 픽 성과추적: 스냅샷을 history로 적립 + --review N(N일 전 매수존 픽 vs 현재가 → 적중률·평균수익) — '스크리너가 실제로 돈 되는 종목을 고르나' 측정, 가중치 조정의 데이터 근거. 검증:test_review_picks+라이브 적립·리뷰
212. ✅🔬 J7 실용판 — weekly.sh 주간 리뷰 원커맨드: report(룰별 성과)+equity+픽 적중률+원장 대사+Serenity 다이제스트 일괄. 검증:스크립트 실행
213. 📋 [의도적 종결] BACKLOG3 잔여 16건 스킵 결정: B6/B7/B8(소액계좌 무관·클라이언트 리스크)·D5(안전필터 완화 반대)·F9/J1/J8(미용)·K7(과잉)·I3/I4(캐시로 해소)·I6(내부)·F10(불가)·기타 — '숫자 채우려 억지 금지' 원칙 적용. 이후 개선은 실거래 표본 기반 룰 튜닝(--review·--report 데이터)으로 전환.
214. ✅🔬 자가평가 엔진 self_eval.py: 원장·metrics·스크리너 히스토리·경보를 임계값 판정으로 자동 평가(룰별 유효/재검토/표본부족, 스크리너 적중률, 리스크 이벤트) + evals/ 적립 + 데이터 임계 충족 시에만 자동 제안(성급한 룰변경 방지). 매일 05:13 KST(화~토, 장마감 후) 크론 176b600d로 자동 실행. 검증:베이스라인 2회 실행+적립
215. ✅🔬🐛 [자가평가 1회차가 잡은 실버그] 테스트→실데이터 오염: manage 계열 테스트가 실제 index_journal.jsonl에 합성 PORTFOLIO_ALERT 63건·metrics.jsonl에 합성 -10% 행들을 기록해옴 → 평가 데이터 왜곡. 정화(백업 후 저널 81→18줄·metrics 회전) + 근본수정(Base.setUp이 모든 테스트의 JOURNAL을 임시디렉토리로 격리). 검증:스위트 전후 실저널 18→18줄 무오염+무결성 0문제+원장=브로커
216. ✅🔬 [증액 반영] 2026-07-20 +$10 입금(총 ~$97) → BUDGET_USD 78→95·MAX_POSITIONS 4→5·HARD_MAX_DAILY 82→100. 집중도/하드한도 테스트를 상수 상대값으로 재작성(향후 상한 변경에도 테스트 불변). 5번째 슬롯 계획: 개장 시 C(금융·실적 7/14 통과·차기 10/14 등록·MA150위·serenity 무관심) ~$18, 가드 거부 시 MS/GS 백업. 검증:207테스트+ci.sh
217. ✅🔬 [사용자 위임] 장외 예외진입 경로(--buy-ext): 평시=정규장 온리 유지, 예외=프리/애프터마켓에서 ①당일 -3%↓ 급락 디스로케이션 ②스프레드 ≤0.35%(fail-closed: 호가 미확인이면 거부) ③지정가(현재가+0.1% 캡)만 — '너무 심하면 들어가라' 재량을 안전레일과 함께 코드화. 다른 가드(예산·실적·쿨다운·집중도 등) 전부 동일 적용. 검증:TestExtEntry 4종+ci.sh
218. ✅🔬 [사용자 챌린지가 잡은 오선정] 5번째 슬롯 C→MS 교체: C는 금요일 매수존이었으나 최신 데이터서 관망 전락(RSI37·50선-3.2%·5일-7.9% 실적후 나이프) — 재검증서 탈락. MS는 매수존(RSI48·+2.0%·점수37.4·5일-3%)+7/15 역대급 실적(비트·배당15%↑) 통과. SECTORS맵 MS/GS/V/MA 등 금융 보정(기타 오분류). 검증:비교표+실적 SEC 8-K+ci.sh
219. ✅🔬 [사용자 제안] 2단 스크리닝: S&P500 전체(498) 하루 1회 딥스캔(--daily-scan, 평일 21:41 크론)→상위 80 동적 유니버스, 30분 사이클은 그 80만(전체를 보되 API 비용 동일). 36h 노화 시 정적 폴백. 부트스트랩: 498→328 통과→80 저장. 검증:TestDynamicUniverse 3종+라이브 스캔+사이클 라벨 확인
220. ✅🔬🚨 [점검이 잡은 크론 사망] 관리 크론(c6a0d47c)이 7일 수명 만료로 소멸해 있었음 — 사용자 "계속 해야지" 계기로 발견, 최신 상태(5슬롯·MS계획·신규 청산룰) 반영해 재등록(48141480). last_run 노화경고(F8)가 이런 상황의 상시 감시장치임을 확인.
221. ✅🔬 [재검증 산출] S&P500 전수 섹터맵(sp500_sectors.json, GICS→한글 500종목)을 SECTORS에 병합(수동 우선) — 동적 유니버스 종목의 '기타' 오분류로 섹터중복 가드·D2 태그가 무력화되던 갭 해소. GS 실적일(10/15) 등록(백업 후보 완전검증). 검증:LNT/DTE=유틸 등 스팟체크+ci.sh
222. ✅🔬 [사용자 승인 A안] OS 레벨 매도보호 크론: Claude 세션 무관 손절/청산 보호 공백 해소 — 토스 API에 스톱주문 부재(LIMIT/MARKET만) 확인 후, 클라우드(키 유출 리스크) 대신 맥 crontab(:23/:53, 22~05시)으로 manage(매도 전용) 상시 실행. config가 .env 자체 로드라 셸 무관 동작, execute.lock으로 세션 사이클과 동시실행 자동 방지, 로그 oscron.log(5MB 회전). 신규 매수는 세션 온리 유지(반자동 원칙). 검증:crontab 등록+래퍼 라이브 1회 실행(4종목 정상 관리)
223. ✅🔬🚨 [적대적 감사 — 실주문 전 15건 확정 수정] 5관점 병렬 버그헌트+반박우선 검증(15실재/2반박). 치명: ①--buy-ext가 락 목록에 없어 단일실행락·저널백업 없이 실주문(락 추가) ②서버 불변식11이 장외를 차단해 --buy-ext가 애초에 작동 불가(ALLOW_EXTENDED_ORDER in-process 플래그로 전달, MCP는 항상 정규장 강제) ③재폴링 승격 시 exitCat 소실→PTP가 러너를 반복 매도(원본 컨텍스트 승계) ④취소 전 부분체결분 미기록→유령주식(선기록 후 잔량종결) ⑤cum 전량기록 주문이 영구 pending→슬롯 잠식(종결 이벤트) ⑥재진입 시 entryTs 과거 고정→시간청산/트레일링 오발동 ⑦_J_CACHE 키 섀도잉으로 캐시 사문화 ⑧sell 실패 시 포트폴리오서 종목 통째 누락(rc 확인) ⑨갭관통 경보 매사이클 중복 ⑩트레일링이 목표/RSI 라벨 가림 ⑪1분봉 신선도 미검증→전일봉으로 진입판정(30분 초과 시 대기) ⑫스크리너 캐시 세션경계 무효화 ⑬DUPLICATE 비종결을 FILLED로 오기록 ⑭서버 naive 타임스탬프 UTC 오가정(KST+음수하한) ⑮딥스캔 품질하한·보유조회 fail-open 경고·리뷰 스냅샷 선택. 검증:TestAudit223Fixes 6종+221테스트+라이브 원장일치
224. ✅🔬🚨 [1차 감사 최종보고가 지적한 '수정의 구멍' 2건] ①buy()/sell() '직접' 경로는 여전히 취소 전 부분체결분을 버리고 있었음(나는 _repoll_pending만 고쳤음) → 양쪽 직접 경로에도 선기록+REMAINDER_CANCELED 적용, sell은 rc=1 반환해 manage가 보유로 계상·재청산하게 함 ②REPLACED를 CANCELED와 동일 취급해 슬롯 해제 → 정정주문은 새 orderId로 생존 가능하므로 종결 금지·슬롯 유지·ORDER_REPLACED 경보로 수동확인 유도. 검증:test_direct_buy_partial_then_cancel_records/test_replaced_keeps_slot+223테스트
225. ✅🔬🚨 [개장 중 응급수정 — 2차 감사 critical 3건] ①체결가 미상 부분체결에 '주문 전액(usd)'이 원가로 들어가 진입가 배수 부풀림→즉시 가짜 손절: 원가 우선순위를 실체결가→실체결금액→cum 기준 수량비례 안분→0(과대 절대금지)으로 재작성 ②manage가 lastPrice를 무검증 신뢰해 0/이상치 한 틱에 전량 시장가 청산 가능: 0·음수·진입가 대비 ±50% 밖이면 피드오류로 간주해 해당 종목 스킵 ③(내 1차 수정이 만든 버그) _repoll_pending의 usd ctx 승계가 부분체결 원가를 부풀림 → exitCat만 승계하도록 축소. 검증:223테스트+라이브 원장일치+MS 신규체결 정확
226. ✅ [실거래] MS(모건스탠리) 0.084236주 @ $213.44(~$18) 매수 — 5번째 슬롯·금융 신규섹터. 진입 근거: 실적 7/15 통과(차기 87일 뒤)·RSI48 매수존·MA150($187.72) 위·당일 -1.0% 눌림(추격 아님)·스프레드 0.094%. 목표 $224.10(+5.0%) 설정. 원장=브로커 일치 확인. 포트폴리오 5종목 $94.88(반도체·에너지·유틸·산업재·금융)
227. ✅🔬🚨 [2차 심층감사 — 확정 17건 중 미수정 9건 장중 수정] 83에이전트·2표검증(반박담당+라이브발생가능성담당). ①원가계상 재수정: 내 1차 안분식이 cum==fq인 첫 부분체결에서 여전히 주문전액을 계상 → 체결가 미상 부분체결은 '아무것도 더하지 않음'으로 확정(과소는 보수적, 과대=가짜손절 절대금지) ②_realized_trades도 동일 usd 날조 제거(실현손익·서킷·쿨다운 오염) ③서킷 손실청산 횟수를 FIFO 로트행→매도이벤트 단위로(1회 매도가 2로트 걸치면 조기 매매중단되던 것) ④좀비 pending: 종결가드가 event명(_PARTIAL)에만 걸려 WORKING→PARTIAL 주문이 영구 미해결→재폴링 예산(6) 잠식 → anyPartial 플래그+_recorded_fill_qty 저널진실 판정 ⑤목표가 익절에 pl>0 가드(낡거나 역전된 목표가가 신규 포지션을 즉시 손실청산) ⑥entryTs 갱신 임계 1e-9→1e-6(가시성 임계와 일치) ⑦manage 종목별 except가 청산판정을 조용히 삼킴 → MANAGE_ERROR 저널+경보 ⑧_reconcile_fill 비-dict 응답 방어 ⑨buy/sell의 reconcile 호출을 try로 감싸 '주문 나갔는데 기록 없음'(유령) 원천차단(예외 시 WORKING 기록+HALT). 검증:TestAudit227Fixes 6종+230테스트+라이브 5종목 원장일치
228. ✅🔬🛡️ [품질가드가 실제로 방어한 사건 + 근본원인 수정] 딥스캔 재실행이 통과 328→42로 붕괴(250종목 지점부터 하드스톱) → 어제 넣은 품질 하한(#223)이 열화된 유니버스 덮어쓰기를 차단, 기존 80종목 무손상 유지. 근본원인: 개장 중(10:51 ET) 498건 연속조회가 레이트리밋에 걸림(장마감 15:39 실행 땐 328 통과). 수정: ①적응형 스로틀(기본 0.15s, 연속 5실패 시 간격 2배+15s 쿨다운, 회복 시 원복) ②개장 중 실행 시 경고 ③진행로그에 실패수·현재간격 표시 ④딥스캔 크론을 개장전 21:41→장마감후 06:07(API 한산)로 이동. 검증:230테스트+유니버스 파일 무손상 확인
229. ✅ [증액 반영 2차] 2026-07-21 +$18.6 입금(총 ~$115.6, 매수여력 $20.76) → BUDGET_USD 95→113·MAX_POSITIONS 5→6·HARD_MAX_DAILY 100→118. 6번째 슬롯 후보 선정: 미보유 섹터 8개 후보 중 실적일 전수 검증 → MGM(최고점수 43.7)은 실적 7/29로 8일 뒤라 진입해도 7/28 강제청산(스윙 러웨이 없음) 탈락, TTWO/KDP/WAT/CAH 실적 미검증 탈락 → JNJ 확정(헬스 신규섹터·실적 7/15 통과 검증·차기 10/13 등록·RSI50 매수존·MA150 +7.0%·ATR2.6% 저변동·serenity 무관심). 검증:230테스트+전 가드 사전통과 확인
230. ✅🔬🚨 [사용자 질문("장이 왜이래")이 잡은 라이브 무력화 버그] 전 종목 당일변동이 0.00%로 죽어 있었음 — 원인은 어제 내 수정(#T15)이 진행중봉 판정을 'ET거래일 동일비교'로 바꾼 것인데, 토스는 최신봉을 KST 날짜로 스탬프해(ET 7/20 장중에 봉 라벨이 2026-07-21) 항상 불일치 → 최신봉을 완료봉으로 오인 → 기준가=현재가 → day_change=0. 영향: 추격가드(>+2.5%)·장외 급락감지(--buy-ext -3%)·미상실적 갭가드가 전부 눈먼 상태였음. 수정: '날짜≥ET거래일' OR '종가≈현재가(라이브추종)'면 진행중봉으로 판정(이중 안전망). 검증:test_kst_stamped_bar_still_detected/test_price_tracking_bar_detected+232테스트+라이브 지수 정상 복구(SPY+0.23% QQQ+0.58% SMH+1.00%)
231. ✅🔬 낙폭서킷 테스트 상수 상대화: 예산 78→113 증액으로 임계(-0.15×예산)가 올라가 하드코딩 -15 손실이 더는 서킷을 건드리지 않아 실패 → 손실폭을 BUDGET_USD에서 계산하도록 수정(집중도·하드한도 테스트와 동일 원칙, 향후 증액에 불변). 검증:232테스트 전체 통과
232. ✅🔬🚨 [사용자 "수정할건 없지?" 검증이 잡은 2건 — 둘 다 '있는 줄 알았던 보호장치의 사문화'] ①부분익절(PTP) 구조적 사문화: PTP 임계는 +5%인데 목표가 설정 관행이 '진입×1.05'라 목표가 전량청산이 if/elif에서 먼저·동시에 걸려 PTP가 영구 미발동 — 라이브 5종목 중 3종목(NEE +4.1%/DE +5.0%/MS +5.0%)에서 실측 확인. '절반 익절 후 러너 유지'는 목표까지 실여유가 있을 때만 성립하므로 _ptp_armed(entry,tgt) 무장가드 신설(목표가가 PTP+PARTIAL_TP_MIN_GAP=3.0%p 이상 위일 때만 무장, 목표 미설정이면 무장). 죽은 분기를 명시적으로 끄는 수정 — $19 포지션의 절반($9) 매도는 이 계좌 규모에선 실익도 없음. ②MCP 장외주문 구멍: 캘린더 백스톱(#223 토스 isHoliday 오작동 교정)이 sessions=[]로 덮어써 safety.check_market_open의 세션 시간검사를 통째로 스킵시킴 → 장전/장후에 캘린더가 오작동하고 실체결이 있으면(유동종목은 장외체결 발생) MCP 주문이 정규장 제한 없이 통과. _et_regular_now()(ET 평일 09:30~16:00, tz DB 없으면 fail-closed)로 백스톱 적용범위를 '실제 정규장' 또는 '의도된 --buy-ext(ALLOW_EXTENDED_ORDER)'로 제한. 재발방지: 과거 2회(_is_us_regular_now·_ptp_armed) 반복한 '호출했으나 미정의' 실수를 AST 스캔으로 8개 모듈 전수 점검(0건). 검증:TestPtpArmed 3종+TestBackstopSessionScope 1종+236테스트+ci.sh+selftest+라이브 --manage 5종목 정상
233. ✅🔬🚨 [1분봉 포렌식이 발견한 단일 방어선 — 장외 유령체결발 가짜 손절] 실측: DE 2026-07-18 05:55 KST에 **거래량 3주**로 473.49(진입 596.20 대비 -20.6%) 체결 후 같은 분봉이 598.00 마감, NVDA 2026-07-21 06:10에 189.54(-8.4%, 손절선 관통). lastPrice는 '마지막 체결가'라 유령체결 한 건이 그대로 남고, 진입가 대비 ±50% 위생검사(#225)를 둘 다 통과한다 → 그 순간 사이클이 돌면 전량 시장가 가짜 손절. 지금까지 이걸 막은 유일한 장치가 '정규장 밖 매도보류' 하나뿐이었고(단일 방어선), 어제까지는 캘린더 백스톱이 그마저 우회 가능했다(#232에서 차단). 수정: _price_corroborated(sym,cur)가 매도 직전 호가창과 대조 — 최우선매수호가가 현재가보다 PRICE_BOOK_TOL(3.0%) 넘게 위면 유령 저가, 현재가가 최우선매도호가보다 3% 넘게 위면 유령 고가로 판정하고 청산 보류(EXIT_UNCORROBORATED 저널+경보, 다음 사이클 재확인). 호가 조회 실패는 1회 재시도 후 fail-closed — 유령 매도는 즉시 확정돼 되돌릴 수 없지만(DE라면 -20% 실현 ≈ -$2.75) 지연된 손절은 30분 뒤 만회 가능하기 때문. 가격기반 청산(STOP/GAPSTOP/TREND/BREAKEVEN/TRAIL/TARGET/RSI/PTP)만 게이팅하고 EARN/TIME은 날짜 기반이라 제외(이득 없이 실패경로만 늘어남). 갭관통 경보를 교차검증 뒤로 이동(유령가로 '급락 갭' 가짜 사건이 원장에 남아 사후분석을 오염시키던 순서 문제). Base.setUp에 기본 호가 목킹 추가(게이트가 fail-closed라 목킹 없으면 전 청산테스트가 막힘 — 파손 자체가 fail-closed 작동 증거). 검증:TestPriceCorroboration 9종(실측 DE/NVDA 수치 그대로 재현+진짜급락 대조군+실적청산 비게이팅)+252테스트+ci.sh+라이브 --manage
234. ✅🔬 [보유분 분석이 드러낸 설계공백 — 저변동주에서만 죽어 있던 트레일링] 보유 5종목이 전부 최고 +0.24~3.63%에서 되밀렸는데(최고점 대비 반납 합계 $2.94) 어떤 룰도 걸리지 않았다: 트레일링 무장 +6%·PTP +5%·목표가 ~+5%라 진입~+5%가 무방비. 원인 진단에서 고정 +6%가 종목마다 전혀 다른 사건임을 확인 — 실측 ATR 기준 NEE는 3.5배(사실상 도달불가) vs NVDA 1.7배(흔한 움직임). 즉 저변동주에서만 구조적 사문화. 수정: _trail_arm_pct(atr)로 무장선을 ATR×TRAIL_ARM_ATR_MULT(1.5)로 정규화, 상한은 종전 TRAIL_ARM_PCT(6.0)라 **어떤 종목도 예전보다 늦게 무장하지 않고**(보호는 넓어지기만) 하한 TRAIL_ARM_FLOOR(2.0)로 노이즈 처닝 방지, ATR 미상이면 종전 고정값 폴백. 라이브 적용 결과 무장선 NEE 6.0→2.6·XOM→3.2·DE→4.6·MS→5.2·NVDA→5.4, 5종목 전부 보유 판정 유지(즉시 매도 없음). **채택하지 않은 것**: 백테스트상 고정 +2.5%(+$1.13)나 ATR×0.8(+$0.74)이 더 좋아 보였으나 (a)8거래 중 5건이 미청산이라 '반납'이 시가평가일 뿐이고 오늘자로는 조기청산 룰이 무조건 유리해 보이는 선택편향, (b)k=0.8은 되고 1.0은 무변화처럼 이득이 전부 0.15%p 간발차에서 나오는 과최적화 signature — 표본과 무관하게 논리적으로 틀린 '변동성 미정규화'만 고치고 파라미터 피팅은 거부. 검증:TestTrailArmScaling 7종(상한불변·하한·폴백·저변동 보호개시·고변동 무변화·목표가 우선)+252테스트+라이브 읽기전용 판정확인
235. ✅🔬🚨 [적대적 검토가 잡은 #233 자체의 결함 — critical 1 + high 6] 내가 방금 넣은 호가 게이트를 4관점 병렬 검토+반박우선 2표 검증에 걸었더니 실제 결함이 나왔다. ①**critical 청산경로 영구소멸**: pl이 스탑 아래인 한 _exit_decision은 항상 STOP을 반환하고 STOP이 게이팅되므로 TIME 청산 분기에 영원히 도달 불가 — 유니버스 80종목 중 75종목은 실적일 미상이라 EARN 백스톱도 없어 호가 API 장애 시 자동청산 0개. UNCORROBORATED_MAX_STREAK(3, ≈90분) 연속 보류 시 fail-closed 해제·강행(EXIT_FORCED_STALE_BOOK) — 유령체결은 단발이라 3연속 재현 불가, 3연속은 '호가 API 사망'의 동의어이므로 종전 정책으로 복귀하는 게 손실 상한이 있는 쪽. ②**TIME이 게이트 우회**: 내 주석은 "TIME은 날짜 기반"이라 적었으나 실제 조건은 days_held AND pl(=cur 파생) — 유령 저가가 스탑선 위(-8%~0%)에 찍히면 STOP/TREND/BREAKEVEN/TRAIL을 전부 비껴가 TIME으로 무게이트 시장가 매도(실측 NVDA 189.54가 정확히 이 구간). _PRICE_DRIVEN_EXITS에 TIME 추가, 주석을 'EARN만 순수 날짜'로 정정. ③**양방향 검사가 오차단**: STOP/GAPSTOP/TREND/BREAKEVEN/TRAIL은 cur가 낮아야 발동하므로 ask쪽 검사는 진양성 0·오차단만 — 진짜 급락 중 체결가가 호가보다 위일 때(548 vs 528/529) 진짜 -8% 손절을 막던 것 실증. side("down"/"up")별로 한쪽만 검사. ④**기각한 유령가로 포트폴리오 평가**: tot_val += cur*qty가 방금 가짜라 선언한 값을 쓴다 → DE급 유령 하나로 -1.7%가 -5.13%로 계산돼 PORTFOLIO_ALERT 당일 1회 래치를 소모, 3시간 뒤 진짜 -6% 하락 경보가 삼켜짐. 호가 mid로 평가하고 mid도 없으면 원가중립(시세실패 경로와 동일 규약). ⑤**빈 호가는 재시도 안 됨**: 재시도가 예외에만 걸려 개장직후 미형성·정지후 재개 등 훨씬 흔한 일시조건이 즉시 fail-closed. bid·ask 모두 유효할 때만 break. ⑥**에스컬레이션 부재**: EXIT_UNCORROBORATED를 _ALERT_EVENTS에 등록(+EXIT_FORCED_STALE_BOOK), _uncorroborated_streak을 _sell_fail_streak과 동형으로 추가, 경보에 연속횟수 표기. ⑦교차/역순 호가(bid>ask) 피드이상 차단 — 가드가 조용히 fail-open 되던 것. 배운 것: 안전장치를 넣는 변경 자체가 새 단일점을 만들 수 있고, 검토 없이 배포한 건 순서 착오였다(#233은 검토 전 라이브 반영됨). 검증:TestAudit235Fixes 10종(side별 오차단 대조군·빈호가 재시도 카운트·TIME 게이팅·유령가 미평가·스트릭 강행/미달 대조군·스트릭 리셋)+262테스트+ci.sh+라이브 --manage+원장 0문제
236. ✅🔬 [사용자 위임 — 장외 목표가 익절 대칭 개방] "애프터마켓에서 목표가 도달하면 안 나가지는거야?" → 그렇다(매도는 tradable=정규장에서만). --buy-ext(장외 급락 진입)와 대칭으로 장외 익절만 조건부 개방(손절/추세/트레일링/PTP는 정규장 전용 유지 — 사용자 선택). 핵심 판별자: 장외 lastPrice의 '목표 도달'은 유령 프린트일 수 있어(실측 XOM 7/18 33주 152.00 프린트 후 147 복귀·감사#232) '실제 최우선 매수호가 ≥ 목표'일 때만 실행 — 그 가격에 사줄 사람이 실재하면 목표 지정가 매도가 즉시 체결되고, 호가가 목표 미달이면 지정가는 미체결로 남을 뿐 손해 없음. _ext_exit_ok(sess,spread,best_bid,tgt): ①프리/애프터 세션 ②스프레드 확인+타이트(EXT_MAX_SPREAD_PCT, fail-closed) ③매수호가≥목표. sell()에 ext/limit_price 추가(장외는 지정가만, ALLOW_EXTENDED_ORDER로 서버 불변식11 통과·finally 복원). manage() 비-tradable 분기에서 cat==TARGET·tgt 있을 때만 게이트 통과 시 sell(ext=True,limit_price=tgt). 라이브 검증: 애프터마켓 --manage에서 XOM 목표 도달했으나 매수호가 151.4<목표 151.97 → 정확히 보류(유령 방어 작동). 범위 한정: RSI 익절(66)은 장외 미지원(가격트리거 아니라 지정가 근거 없음·사용자는 '목표가'만 요청) — 정규장에서만. 검증:TestExtExit 9종(게이트 세션/호가/스프레드 경계+manage 실매수 익절/유령보류/손절 비대상)+270테스트+ci.sh+selftest+라이브 무주문 확인
237. ✅🔬🚨 [실거래 시도가 밝힌 근본 제약 — 소수점은 정규장 전용, #236 전면 철회] 사용자 "152.24까지 갔는데 뭐해?"(XOM 목표 위 야간 도달) 조사 중 발견: 토스는 미국 소수점 주문을 정규장(KST 22:30~05:00)에만 접수한다. 두 단계로 확인 — ①소수점 지정가 매도 거부 "소수점 수량 주문은 미국 주식 시장가 매도 주문에만 사용"(invalid-request), ②소수점 시장가 매도도 장외선 거부 "미국 주식 소수점 수량 주문은 정규장 시간에만 접수"(fractional-quantity-outside-regular-hours, regularHours 22:30~05:00 KST 명시). 우리 6종목 전부 소수점(계좌 $115, 슬롯당 $18)이라 장외 매매는 주문유형 불문 원천 불가. → #236(장외 목표가 익절)은 잘못된 전제(장외 매매 가능)로 만든 기능 — 우리 계좌에선 절대 발동 불가하므로 전면 철회: _ext_exit_ok 삭제, sell()의 ext/limit_price/ALLOW_EXTENDED_ORDER 래핑 제거(매도는 항상 시장가), manage() 비-tradable 분기를 원래의 '보류만'으로 복원, TestExtExit 9종 제거. 크론도 원복(장외 커버는 무의미 — OS 22~05시/세션 22~04시). 교훈: 안전레일이 3번(우리 세션게이트·소수점지정가·소수점장외) 저항한 건 전부 실제 플랫폼 제약을 반영한 것이었고, 원래 설계(tradable=정규장만)가 옳았다. 실주문 시도 2건은 전부 거부(체결·손실 0). 함의: 소수점 계좌는 손절도 정규장에만 실행 가능 — 장외 갭다운은 정규장 개장까지 막을 수 없음(구조적 한계, 사용자 인지 필요). 검증:262테스트+ci.sh+selftest+실주문 거부 원문 확보
238. ✅🔬 [소수점 장외 매매불가 → 야간 알림] 감사#237로 소수점은 정규장에만 매매 가능 확정 → 봇은 장외에 못 팔지만, 사용자는 앱에서 24시간 수동매매 가능(앱은 야간거래 지원). manage() 비-tradable 분기에서 청산 트리거가 '진짜'면(목표: 실매수호가≥목표 / 손절: 호가 교차검증) EXIT_SIGNAL_OFFHOURS 발화 → macOS 알림으로 "앱에서 수동매도 검토" 통지. 종목당 하루 1회 dedup(_offhours_alerted), 유령 프린트엔 침묵. manage_cron.sh에 TOSS_DESKTOP_ALERTS=1 export, OS크론 17~08시 재확장(정규장 거래+장외 알림). 검증:TestOffhoursAlert 4종(실트리거 알림/dedup/유령침묵/alertable)+266테스트+ci.sh
239. ✅🔬🚨 [핵심 발견 — 토스에 조건부 주문 존재, "스톱주문 없음" 전제가 틀렸음] 사용자 "앱에선 되는데?" 도전으로 공식 스펙(openapi.json v1.1.1) 정독 → /api/v1/conditional-orders 발견: SINGLE/OCO(손절+익절 동시,하나 체결시 나머지 취소)/OTO, 조건 STOP(가격트리거)·PROFIT_RATE(목표수익률), 서버측 24시간 감시. 우리 시스템 전체가 "토스 API엔 스톱주문 없어 폴링에만 의존"이란 틀린 전제 위에 있었음. 실검증(비발동 조건 생성→DELETE): ①소수점 조건부주문 거부(정수주만) ②정수주 수락 ③취소는 DELETE /conditional-orders/{id}(/cancel 아님). 함의: 소수점 계좌(우리 전략 6종목)는 조건부주문 못 쓰지만, 통주 레거시(MBRX 56주 등)엔 사용 가능. 잔여 테스트주문 2건 즉시 정리(원장무손상). [사용자 지시] MBRX(멀레큘린) 56주에 본전 조건부 매도 설정: 평단 $3.56·현재 $2.15(-39.6%)·"손해 절대 안됨" → triggerPrice/orderPrice $3.60(본전+, 비용후 무손실)·LIMIT(페니주 스프레드 슬리피지 차단)·56주·만료 2026-10-20·SINGLE SELL. 상태 WATCHING 확인(id msPbG4h…). 정직 고지: 조건부주문은 '손실확정 방지'일 뿐 '손실 방지' 아님 — 회복 못하면 미발동, 하락 지속 위험 실재.
240. ✅🔬 [타이밍·종목 감사가 발견한 설계 갭 — 저변동주 진입 차단] 사용자 "매수매도 타이밍·종목 적절한지 확인" 감사 결과: ①매도 타이밍 A급 — GOOGL 청산 익일 실적 -14% 붕괴 회피(기대감 매도 가설의 결정적 실증), MRK/XOM은 청산 후 +2.4~2.7% 양보(보험료 수준), 룰 무수정 결론 ②매수 타이밍 B급 — 평균 MAE -2.2%(≈1 ATR 눌림 후 회복), 지정가 개선은 소수점=시장가 전용이라 불가, 표본 4건에 추가 조임은 과최적화라 미조치 결론 ③종목 선정 B+ — 6보유 전부 추세·매수존 유지, 단 **목표가 ATR 배수 분석에서 구조 갭 발견**: 승리 3건은 목표가 1.3~2.4 ATR(1~7일 도달), NEE만 3.0 ATR로 7거래일 죽은 돈 — 저변동주에 +5% 일괄 목표가 통계적으로 과도(트레일링 #234와 동일 병리의 진입판). 수정: ENTRY_MIN_ATR_PCT=2.0 신규진입 하한(_entry_guard) — +5% 목표 ≤2.4 ATR 보장, XOM(2.13) 통과·NEE(1.65)류 차단, ATR 미상은 미차단(과차단 방지). 기존 NEE는 자르지 않고 시간청산(20거래일)에 위임. 검증:TestEntryAtrFloor 3종+269테스트+ci.sh
241. ✅🔬 [사용자 "장 시작하자마자 즉각 매수, 느릿느릿해서 다 오르고 산다" — 개장 즉시 매수엔진] 완전자동형 선택. 느린검증(개장전)/빠른체결(개장순간) 분리: 개장 전 검증·승인된 pending_buys 큐를 개장창(22:30~40) 1분 크론이 exec_open_buys로 buy()에 발사. buy()의 전 게이트(추격차단·실적·매수존·ATR≥2.0·예산·과집중) 그대로 통과 — 매도 개장1분감시(XOM)와 대칭. rc=0체결→제거/2대기→유지/3HALT→중단/1거부→제거. CLI --arm-buy/--disarm-buy/--list-armed/--exec-open-buys(락목록 추가). 검증:TestOpenBuyQueue 6종+275테스트. dormant 상태로 배포(큐빔·크론미등록·슬롯만석).
242. ✅🔬🚨 [무장 전 적대적검토가 잡은 배포차단 3건 — 43에이전트 2표검증] #241을 무장 전 검토→critical/high 3건 확정, 무장 금지 판정. ①B1(critical) HALT/kill-switch가 큐를 '보존' 아니라 '전량삭제': exec_open_buys 유일가드가 tradable뿐이라 HALT 시 buy()가 rc=1→else분기가 _disarm+continue로 한 틱에 큐 전멸. 비상정지 touch HALT가 큐를 멈추는 게 아니라 지움. → 루프 진입 전 HALT/allow_live_orders 검사(큐보존 return)+틱중간 HALT 재확인(삭제 전 break). ②B2(high) 매수여력부족 rc=1 영구삭제 — 오늘밤 입금 FX환전 지연 시나리오 직결: 개장창 첫틱에 환전 미반영이면 bp<usd→rc=1→큐삭제, 몇분뒤 반영돼도 0체결. → 매수여력부족을 rc=2(대기·재시도)로 재분류(buy() 전역). ③B3(critical) 실적 웹검증없이 큐 채우면 안 됨 — 판정 NO: 실적가드가 earnings_dates.json(13종목)+미상갭4%뿐, 유니버스 80중 미등록은 _days_to_earnings=None→실적블록 스킵→그날아침 실적발표 종목을 자동매수. → _arm_buy가 실적일 미상/레거시 fail-closed 거부(arm시점)+발사시점 재확인. 운영요건: 자동선정기는 각 후보 실적 웹검증→--set-earnings→--arm-buy 강제. 부수(L1 stale 무장일 스탬프+폐기·L2 arm/disarm 락목록·L3 큐손상 경보). 검증:TestOpenBuyQueue 13종(HALT큐보존·live-off보존·틱중간HALT·stale폐기·실적미상거부·발사시점재확인)+281테스트+ci.sh+selftest+라이브 arm거부 실증. 교훈:이번엔 검토→수정→무장 순서 준수(dormant라 라이브위험 0인 상태에서).
243. ✅🔬 [사용자 "꼼꼼히 설계 개선" — 변동성 적응 재설계] 리스크 분석서 "달러 균등·리스크 11배 불균형"(GEN 하나가 포트 리스크 44%) 발견 → 사용자 ①+② 선택. ①리스크 균등 사이징: _position_size_usd(atr)=BUDGET×PORTFOLIO_DAILY_RISK_PCT(0.032)/MAX_POSITIONS ÷ (atr/100), 고변동 적게·저변동 많이 담아 슬롯당 일리스크 균등(실측 ~$1.5). [10, 0.2×예산] 클램프. _arm_buy(usd 생략시 자동사이징). ②k×ATR 목표: _atr_target=진입×(1+2×ATR), 고정 +5% 대신 변동성 비례. --suggest-entry READ-ONLY. 검증:TestVolAdaptiveSizing+290테스트.
244. ✅🔬🚨 [무장 전 적대적검토(57에이전트)가 잡은 #243 반쪽작동 — 목표 미배선] 판정: 자본손실 하드블로커 없으나(전 결함 보수적방향) "#243 절반만 작동". 확정 2건 수정: ①[A]목표 미배선(critical 인지불일치): 사이즈만 buy에 배선되고 목표(2×ATR)는 배선 안 돼 자동진입분이 tgt=None→목표익절분기 영영 미발동, 사용자가 믿는 "신규 2×ATR 목표"가 라이브에 부재. → buy() FILLED 직후 set_target(_atr_target(체결가))로 자동배선(수동선설정·부분체결 반복은 보존, 정수벽 넛지, 실패시 TARGET_SET_FAIL 저널). ②[B]저ATR R:R 잠식: 하한 1.0에서 목표 2×ATR(+2~4%) vs 손절 -8% = R:R 0.25~0.5, 스크리너가 저변동 선호(-atr×2.5)라 이 나쁜밴드가 주력산출 → ENTRY_MIN_ATR_PCT 1.0→2.0 복원(ATR≥2%만 R:R≥0.5). PTP 상호작용 검토결과 정상(_ptp_armed가 목표≥PTP+3%일때만 무장 → ATR≥4%만 PTP발동, 저중변동은 목표 전량청산으로 자동정합). 목표vs트레일링(1.5×ATR)도 profitable_tp 가드로 사문화 아님 확인. 나중처리(🟡): divisor 고정9→잔여슬롯(과소리스크·안전), GEN 41% 레거시집중(자연회전 대기), suggest_targets 공식 통일. 검증:TestVolAdaptiveSizing 10종(목표자동배선·넛지)+292테스트+ci.sh+selftest+라이브 suggest-entry
245. ✅🔬 [#244 검토가 남긴 🟡 마이너 3건 정리 — 대기시간 활용] ①사이징 정직화: round→floor(양수 int, 절대 올림 안 함=보수적) + docstring에 "divisor 고정 MAX_POSITIONS라 슬롯별 상한이지 포트폴리오 리스크 강제 아님, 저변동 유니버스에선 달러캡에 먼저 물려 총리스크 목표 미달 가능(과소리스크=안전방향)" 명시. ②목표 도구 통일: suggest_targets가 종전 max(4%,1.5×ATR) 쓰던 걸 buy() 자동배선과 동일한 _atr_target(2×ATR)로 일원화 — 두 도구(--suggest-entry/--suggest-targets)가 이제 같은 목표 제안(GEN 둘 다 +6.6% 확인). ③GEN 레거시 집중(41%): 목표 코앞(+3.8%, 목표까지 0.3%)이라 곧 자연 청산으로 정규화 → 코드 불필요. 나머지 divisor를 '잔여슬롯/포트폴리오 잔여리스크'로 바꾸는 큰 개선은 네트워크(기존 포지션 ATR) 필요+과소리스크가 안전방향이라 의도적 미루기(오버엔지니어링 회피). 검증:292테스트+ci.sh+selftest+두 목표도구 일치 확인. 신규진입 대기(~8/5 실적창)라 라이브 영향 0.
