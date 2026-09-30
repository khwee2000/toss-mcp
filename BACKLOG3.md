# BACKLOG3 — 3라운드 개선 100항목 (2026-07-17 재스캔)

★ = TOP 20 (이번 세션에서 꼼꼼히 구현). 검증 규칙: 항목마다 tests/test_index_executor.py 전체 통과 + py_compile.

## A. 머니-기록/체결 정확성 (MH 클러스터 — 세션 직접 처리)
- [x] ★T1(MH1) 체결가 미상 fill의 읽기시 가격역산: _realized_trades에서 fp/sp≤0이면 filledUsd/fillQty로 복원(lot 유실·P&L 누락·서킷/쿨다운 오염 방지)
- [x] ★T2(MH1b) _reconcile_fill에 filledAmount/fq 평균가 폴백 + SELL 이벤트에도 filledUsd 기록
- [x] ★T3(MH2) PARTIAL_FILLED → BUY_PARTIAL/SELL_PARTIAL 이벤트(cum 누적필드) — 부분체결을 부분으로 기록
- [x] ★T4(MH5) manage() 시작 시 미확정(working/uncertain/partial) 주문 재폴링 → 체결 증분 승격 / 죽은주문 슬롯해제
- [x] ★T5(MH7) 체결기록 멱등화: _recorded_fill_qty(orderId)로 '증분만' 저널(재기록/dup채택 이중계상 원천 차단)
- [x] ★T6(MH6) DUPLICATE 채택 시 기존주문이 CANCELED/REJECTED(체결0)면: buy=슬롯해제, sell=SELL_RETRY(손절 삼킴 방지)
- [x] ★T7(MH4) buy 체결확인 전부실패(st None) → BUY_UNCERTAIN+HALT(미체결 단정 금지, sell SL1과 대칭) + _set_halt(사유기록·경보) 일원화
- [x] ★T8(MH3) stale-coid 이중롱 — T5+T6 조합으로 커버(orderId 멱등) + 회귀테스트로 고정
- [x] A9 SELL_FAIL 후 재시도 백오프(연속 실패 시 간격 증가)
- [x] A10 _reconcile_fill 폴링 지수백오프(1.5s 고정 → 1→2→4s)
- [x] A11 저널 쓰기 fsync 옵션(전원단절 내구성)
- [x] A12 --merge-fallback 명령(fallback 병합 후 안전 삭제)

## B. 주문경로/서버
- [x] ★T9(MH8) MCP 주문경로 락 게이트: execute.py 실행(락 보유) 중 MCP place_order_confirmed 차단(교차 이중매수 방지, execute 자신은 env로 통과)
- [x] B2 서버 감사로그(coid)와 전략저널 조인 리포트
- [x] B3 kill-switch 상태 --status 표시
- [x] B4 MCP 경로 주문 발생 시 out-of-band 알림
- [x] B5 주문 rate-budget 잔여 노출
- [ ] B6 preview 만료 시 1회 자동 재플랜
- [ ] B7 고액가드 임계 재점검(소액계좌 기준)
- [ ] B8 시장가 대신 지정가+틱 슬리피지 캡 옵션

## C. 청산/전략
- [x] C1 트레일링 스탑(MFE 기반 이익보전)
- [x] C2 부분익절(+5% 절반, 잔량 러너)
- [x] C3 RSI 익절에 최소수익 조건 검토
- [x] C4 시간청산 '거래일' 기준 환산
- [x] C5 목표가 ATR 기반 자동 재산정 제안
- [x] C6 레짐 off 시 보유관리 정책 문서화
- [x] C7 섹터 중복 진입 소프트 가드
- [x] C8 실적 통과 후 재진입 규칙
- [x] C9 배당락일 인지(배당주 NEE/XOM)
- [x] C10 손절 후 V반등 재진입 개선(쿨다운 예외 조건)

## D. 스크리너
- [x] D1 실적일 파일 통합 표시(dte 컬럼)
- [x] D2 섹터 태그 출력(분산 판단 보조)
- [x] D3 유동성 하한 상향 검토($20M→$50M)
- [x] D4 RS 다중기간(1M/3M) 합성
- [ ] D5 갭 필터 완화 검토(|3%|→|4%|)
- [ ] D6 유니버스 정기 리뷰 프로세스
- [x] D7 종목별 캔들 캐시(사이클 간 재사용)
- [x] D8 선별 스냅샷 저장(재현성)
- [x] D9 점수 가중치 백테스트
- [x] D10 사이클 간 diff 자동 하이라이트(신규진입/이탈)

## E. Serenity/인텔
- [x] ★T18 아카이브 신선도 경고(sync_state 4일↑ 정지 시 — pre-trade 신호가 조용히 늙는 것 방지)
- [x] E2 티커별 최근 언급 감정 요약(강세/약세 키워드)
- [x] E3 보유종목 신규 언급 시 manage 경고 연동(digest 하이라이트는 E4에 포함됨)
- [x] E4 일일 다이제스트 자동화(--digest: 새 트윗만+보유/워치 하이라이트)
- [x] E5 아카이브 로컬 미러(원본 저장소 소실 대비)
- [ ] E6 보조 소스 추가(공식 IR/뉴스)

## F. 관측/운영
- [x] ★T12 PORTFOLIO_ALERT 1회/ET일 dedup(경보 스팸 방지)
- [x] ★T13 로그 로테이션(execute.log/alerts.jsonl/metrics.jsonl 5MB)
- [x] ★T14 장부 일일 백업(.backups/journal-ETday.jsonl, 14일 보관)
- [x] ★T16 --status 확장(하드일일한도·최대비중·백스톱 상태)
- [x] ★T10 verify-broker 역드리프트(브로커>원장=미기록 수동/MCP 매수 의심) 경고 — MH8 감시 루프 완성
- [x] ★T17 --selftest 확장(targets/earnings 파싱 자체검증 + 보유종목 목표가 커버리지)
- [x] F5 --report 주간 자동 요약
- [x] F6 에쿼티 곡선 아티팩트 차트
- [x] F7 데스크톱 알림 기본값 정책
- [x] F8 last_run 노화 감시(죽은 크론 조기경보)
- [ ] F9 execute.log JSON 구조화
- [ ] F10 세션 종료 시 상태 스냅샷

## G. 견고성/데이터
- [x] ★T11 targets/earnings 파싱실패 경고 노출(silent {} → 익절출구 소실 가시화)
- [x] ★T15 _day_change 진행중봉 판정을 ET날짜 기반으로(0.1% 가격일치 휴리스틱 제거)
- [x] G3 targets 원자적 쓰기 헬퍼
- [x] G4 earnings 원자적 쓰기 헬퍼
- [x] G5 저널 손상라인 복구 도구
- [x] G6 metrics 스키마 버전 필드
- [x] G7 zoneinfo 실패 환경 폴백 재점검
- [x] G8 디스크 풀 시 동작 정의
- [x] G9 데이터파일 퍼미션 일괄 0600
- [x] G10 백업 무결성 체크섬

## H. 테스트
- [x] ★T19 buy/sell 해피패스+부분체결 통합 테스트(플랜→체결→저널 end-to-end)
- [x] ★T20 재폴링·dup멱등·가격역산 회귀 테스트 묶음
- [x] H3 sell 해피패스 세부(클램프 조합)
- [x] H4 서킷 경계값 매트릭스
- [x] H5 manage e2e 부분체결 시나리오
- [x] H6 FIFO property 기반 테스트
- [ ] H7 mock/live 스위치 테스트
- [x] H8 서버 주문게이트 테스트
- [x] H9 골든 저널 스냅샷 회귀
- [x] H10 원커맨드 CI 스크립트

## I. 성능
- [x] I1 _read_journal mtime 캐시
- [x] I2 서킷 내 _realized_trades 2회 호출 축소
- [ ] I3 스크리너 병렬 fetch
- [ ] I4 fetch TTL 캐시
- [x] I5 manage 심볼별 예외 격리 재확인
- [ ] I6 토큰 선제 갱신

## J. UX/보고
- [ ] J1 보고 포맷 함수화
- [x] J2 원화 환산 병기
- [x] J3 수수료 반영 순P&L
- [ ] J4 세금 참고 표시
- [x] J5 --holdings 전략/레거시 구분 강화
- [x] J6 목표대비 진행률 바
- [x] J7 일일 요약 자동 아카이브
- [ ] J8 이모지/상태 뱃지 표준화

## 스킵 결정(2026-07-18 재평가): B6·B7·B8·D5·D6·E6·F9·F10·H7·I3·I4·I6·J1·J4·J8·K7 — 실익<리스크/미용성(숫자 채우기 금지 원칙). 필요 시 재개.

## K. 보안/설정
- [x] K1 토큰 파일 퍼미션 재검
- [x] K2 키 로테이션 리마인더
- [x] K3 config 스키마 검증 강화
- [x] K4 env 오버라이드 감사 출력
- [ ] K5 HALT 사유 파일 기록(★T7에 포함)
- [x] K6 kill-switch/HALT 통합 뷰
- [ ] K7 백업 암호화 검토
- [x] K8 로그 민감정보 스크럽
- [x] K9 의존성 버전 고정
- [x] K10 재해복구 절차 문서
