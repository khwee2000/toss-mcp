# SPEC — 내 돈을 아는 AI 트레이더 (toss-trader MCP)

> Claude × 토스증권 Open API MCP 서버.
> 자연어로 "내 계좌 어때?", "엔비디아 50만원 3분할 매수 플랜 짜줘"를 토스 실계좌/시세에 연결해 처리한다.
>
> **현재 상태: mock 기본. 사용자 토스 키 미발급 → live 비활성. 1차 출시는 조회 전용(write 도구 미등록).**

---

## 1. 프로젝트 개요

| 항목 | 내용 |
|---|---|
| 이름 | toss-trader |
| 형태 | FastMCP(stdio) 단일 프로세스 MCP 서버 |
| 호스트 | Claude Desktop / Claude Code |
| 자격증명 | BYOK (Bring Your Own Key) — 사용자 본인 토스증권 Open API 키 |
| 기본 모드 | **mock** (키 없거나 `TOSS_LIVE!=1`이면 네트워크 차단·fixture 응답) |
| 출시 정책 | 1차 = 조회 전용 → 2차 = dry-run(plan) → 3차 = 실주문(opt-in) 단계적 개방 |
| 기존 자산 재사용 | `~/stock-dashboard/watchlist.json` (관심종목 200개), 지표 로직(RSI·이평선·단타점수) |
| 언어 | Python 3 (stdlib + FastMCP) |

### 설계 철학 — 3-링(READ / PLAN / EXECUTE)
- **READ 링**: 부작용 0. 시세·계좌·주문 조회. `mutates: false`.
- **PLAN 링**: dry-run. 주문을 **절대 생성하지 않고** 검증만 수행, `preview_token` 발급. `mutates: false`.
- **EXECUTE 링**: 실제 POST. `preview_token` + `confirm_phrase` + live 게이트 + 매 POST KILL 재확인. `mutates: true`. `TOSS_ALLOW_LIVE_ORDERS=1`일 때만 등록.

---

## 2. 검증된 토스 Open API 엔드포인트

> 공식 OpenAPI 스펙(`https://openapi.tossinvest.com/openapi-docs/latest/openapi.json`)을 ground-truth로 사용.
> **Base URL:** `https://openapi.tossinvest.com`
> 일부 서드파티 블로그가 `/v1/market/price`로 적었으나 공식 스펙은 `/api/v1/prices`임 → 공식 스펙을 신뢰.

### 2.1 Auth
| Method | Path | 설명 | 검증 |
|---|---|---|---|
| POST | `/oauth2/token` | OAuth2 client_credentials → `{access_token, expires_in}` | 검증됨 (흐름은 §3) |

### 2.2 Market Data (시세 — 계좌 무관, 무료)
| Method | Path | 설명 | 핵심 주의 |
|---|---|---|---|
| GET | `/api/v1/prices` | 현재가 다건 (최대 200종목, `symbols` 콤마구분) | **응답 4필드뿐** — `symbol/timestamp/lastPrice/currency`. 변동률·거래량·OHLC 없음 → candles 합성 필수 |
| GET | `/api/v1/orderbook` | 10단계 호가 (매수/매도 호가·잔량) | 단건 |
| GET | `/api/v1/candles` | 캔들 OHLCV. `interval` 1m/1d, `count`≤200, `before` ISO8601 페이지네이션(`nextBefore` 커서), `adjusted` def true | 지표 입력 원천 |
| GET | `/api/v1/trades` | 최근 체결 (체결가/량/시각) | `count` 1~50 |
| GET | `/api/v1/price-limits` | 당일 상/하한가 (`upperLimitPrice`/`lowerLimitPrice`) | 분할매수 가격밴드 검증 |

### 2.3 Stock Info
| Method | Path | 설명 |
|---|---|---|
| GET | `/api/v1/stocks` | 종목 마스터(검색/검증). 심볼 예: `005930`(KRX), `AAPL`(US) |
| GET | `/api/v1/stocks/{symbol}/warnings` | 매수 유의(정리매매·단기과열·투자경고/위험·VI) |

### 2.4 Market Info (환율·휴장일 — 계좌 무관)
| Method | Path | 설명 | 주의 |
|---|---|---|---|
| GET | `/api/v1/exchange-rate` | KRW↔USD 표시환율(1분 갱신), `validUntil` 노출 | 체결가 보장 아님. **preview TTL = min(120s, validUntil-now)** |
| GET | `/api/v1/market-calendar/KR` | 국내 장 운영·휴장일·세션(KRX·NXT) | NXT 세션 포함 |
| GET | `/api/v1/market-calendar/US` | 미국 장 운영(프리/정규/애프터/데이마켓, KST) | |

### 2.5 Account / Asset (계좌 — `X-Tossinvest-Account` 헤더 필요)
| Method | Path | 설명 | 주의 |
|---|---|---|---|
| GET | `/api/v1/accounts` | 본인 계좌 목록 (`accountNo`/`accountSeq`/`accountType`) | `accountSeq`(integer)를 캐시 → 이후 호출 헤더 자동 주입 |
| GET | `/api/v1/holdings` | 보유종목 상세(수량·평단·평가·손익·일손익 `dailyProfitLoss`·cost) | 다계좌(연금/ISA) 방어 |
| GET | `/api/v1/buying-power` | 통화별 매수가능 금액(현금 기반, 미수 제외) | |
| GET | `/api/v1/sellable-quantity` | 종목별 매도가능 수량 | |
| GET | `/api/v1/commissions` | 시장별 매매 수수료(국내 면제정책 2026.6 만료 가능 → 런타임 조회) | 하드코딩 금지 |

### 2.6 Order (주문)
| Method | Path | 설명 | 주의 |
|---|---|---|---|
| GET | `/api/v1/orders` | 주문 목록(`status` OPEN/CLOSED), execution·페이지네이션(`nextCursor`/`hasNext`) | orderId = 가변길이 opaque 문자열 |
| GET | `/api/v1/orders/{orderId}` | 단일 주문 상세 | reconcile/추적의 진실 원천 |
| POST | `/api/v1/orders` | 주문 생성. `OrderCreateRequest` oneOf(수량기반 vs US 금액기반) | clientOrderId 멱등(10분) |
| POST | `/api/v1/orders/{orderId}/modify` | 미체결 정정. required `[orderType]` | **새 orderId 반환** → 추적 교체 |
| POST | `/api/v1/orders/{orderId}/cancel` | 취소 | **새 orderId 반환** → 추적 교체 |

### 2.7 실측 검증된 스키마 사실 (spec v1.1.1 직접 파싱)
- **OrderStatus = 10개 값**: `PENDING` / `PENDING_CANCEL` / `PENDING_REPLACE` / `PARTIAL_FILLED` / `FILLED` / `CANCELED` / `REJECTED` / `CANCEL_REJECTED` / `REPLACE_REJECTED` / `REPLACED`.
- **modify/cancel은 새 orderId를 반환**(원주문과 다름) → 추적 스냅샷이 stale 될 수 있음. 새 orderId로 추적객체 교체 + `track_order` 재조회 필수.
- **timeInForce**: 생성 요청은 `DAY`/`CLS`만. 응답(Order)에는 `DAY`/`CLS`/`OPG`(OPG는 read-only) → 응답 파서는 OPG 허용.
- **PriceResponse = 4필드뿐**: `symbol`/`timestamp`/`lastPrice`/`currency`. 변동률·거래량·OHLC 없음 → candles 합성.
- **OrderExecution**: `filledQuantity`/`averageFilledPrice`/`filledAmount`/`commission`/`tax`/`settlementDate` 노출(부분체결·세금·결제일).
- **ApiError**: flat **string** code + `data`(nearestPrices/tickSize 등 힌트). **unknown code 허용** 권고(원문 노출).
- **Rate-limit 헤더 실재**: `X-RateLimit-Limit`/`X-RateLimit-Remaining`/`X-RateLimit-Reset`, `Retry-After`.
- **실존 에러 코드**: `confirm-high-value-required` / `opposite-pending` / `prerequisite-required` / `restricted` / `maintenance` / `request-in-progress` / `insufficient-*`.

---

## 3. OAuth2 흐름

```
[1] 토큰 발급
  POST https://openapi.tossinvest.com/oauth2/token
  Content-Type: application/x-www-form-urlencoded
  Body: grant_type=client_credentials&client_id={ID}&client_secret={SECRET}
  → 401 시 Basic-auth 헤더(Authorization: Basic base64(id:secret)) 1회 폴백
  ← { "access_token": "...", "expires_in": 3600 (동적, 하드코딩 금지) }

[2] 토큰 캐싱 (필수)
  - 메모리 + TOSS_DATA_DIR/token.json (0600)
  - 만료 ~10% 전 선제 갱신
  - 401 시 single-flight 락 잡고 1회 재발급 (client당 1토큰 규칙)
  - dashboard와 client_id 분리 (동일 client_id 충돌 시 이전 토큰 무효화 → 401 storm 방지)

[3] 인증 호출
  Authorization: Bearer {access_token}
  + (계좌 호출) X-Tossinvest-Account: {accountSeq}   ← 래퍼가 자동 주입

[4] 계좌 컨텍스트
  시작 시 GET /api/v1/accounts → accountSeq 캐시
  도구에는 account_index(opt, def 0)만 노출, accountSeq는 비노출
```

토큰·secret은 **절대 로그/응답/스키마에 노출하지 않음** (redaction 필터, §6).

---

## 4. 최종 도구 표면 (Tool Surface)

> **도구 카탈로그 = 함수 42개** (READ 31 + PLAN 7 + EXECUTE 4). 여기에 카탈로그명 별칭(동일 backing 함수의 두 번째 이름)을 더해 Claude에 노출.
> - **read-only 모드(현재 출시, mock 기본)**: READ 31 + PLAN 7 + ALIAS 5 = **43개 노출**.
> - `TOSS_ALLOW_LIVE_ORDERS=1`: 위에 EXECUTE 4 + WRITE ALIAS 1 = **48개 노출**.
> - 세는 단위는 **MCP에 등록되어 Claude가 호출 가능한 도구 이름 수**다. 함수 42개 + 별칭(read 5 / write 1)이 노출 수의 차이를 만든다.
> READ(조회) / PLAN(dry-run) / EXECUTE(실행) 3링.
> **EXECUTE 도구는 `TOSS_ALLOW_LIVE_ORDERS=1`일 때만 등록** (1차 출시엔 미등록).
>
> **별칭(카탈로그명 → backing 함수)**: `portfolio_snapshot`→`portfolio_overview` · `plan_split_buy`→`plan_split_order` · `price_quote`→`get_prices` · `candles`→`get_candles` · `preview_order`→`plan_order`(preview_token 발급) · (live only) `place_order`→`place_order_confirmed`. 별칭은 새 도구가 아니라 같은 함수의 추가 이름이므로 카탈로그 행을 따로 만들지 않는다.
>
> **공통 결과/에러 스키마 — 2 스키마 공존(SSOT 명시)**: 신규 alpha 도구(§4.2 `stock_info` · §4.4A · §4.5A)는 성공 `{ok:true, data, _mock}` / 실패 `{ok:false, error:{code, message_ko}}` 봉투를 쓰고, 원본 26개 도구(§4.1·§4.2 외 시세·§4.3·§4.4·§4.5·§4.6 기존 행)는 **bare dict + `_mode`**(성공은 페이로드 평탄·`_mode` 표기, 실패는 `{error:{code,message}, _mode}`) 형태를 유지한다. 두 스키마는 현재 의도적으로 공존하며, 단일화되어 있지 않다(거짓 단일화 주장 금지).

### 4.1 합성 READ (자연어 1콜 처리)
| 도구 | 설명 | RW | 안전장치 | 매핑 |
|---|---|---|---|---|
| `portfolio_overview` | "내 계좌 어때?" 1콜. accounts→holdings→buying-power→prices→commissions 합성. 계좌요약(평가/매입/총손익/일손익·KRW·USD)·종목별 보유·매수가능액·수수료 | READ | 부작용0, accountSeq 비노출, 멀티콜 rate-limit 최소화, `_mode` 표기 | accounts+holdings+buying-power+prices+commissions (합성) |
| `analyze_symbol` | "엔비디아 분석해줘" 1콜. stocks+warnings+get_quote 합성 + indicators(RSI·이평선 20/60/120·단타점수·체결강도) | READ | on-demand 단건(폴링 금지), warnings 위험 플래그, 외국인수급은 토스 미제공→별도 출처 표기 | stocks+warnings+prices+candles+indicators (합성) |

### 4.2 단건 READ — 시세
| 도구 | 설명 | RW | 안전장치 | 매핑 |
|---|---|---|---|---|
| `get_quote` | 단일 종목 종합 시세 카드. prices(4필드)+candles 합성→전일대비·변동률·거래량·OHLC 보강 | READ | prices+candles 2버킷, 단건 | `/prices` + `/candles` (합성) |
| `resolve_symbol` | 자연어 종목명→심볼·시장(KR/US)·통화. watchlist+stocks 교차검증. 모호하면 후보만 반환(오발주 방지) | READ/로컬 | 다중매칭=후보 반환·확정 안 함 | 로컬 + `/stocks` 검증 |
| `get_prices` | 현재가 다건(`symbols` 콤마). 관심종목 200개 1콜 묶음(폴링 절약). 응답 4필드뿐 | READ | 다건 batch 우선으로 rate-limit 보호, ORDER 쿼터 분리 | `/api/v1/prices` |
| `get_orderbook` | 단일 종목 호가(매수/매도 호가·잔량) | READ | 단건 only, 폴링 루프 금지 | `/api/v1/orderbook` |
| `get_candles` | 단일 종목 캔들 OHLCV. 1m/1d, count 1~200, before 페이지네이션, adjusted | READ | MARKET_DATA_CHART 별도 버킷, nextBefore 커서 | `/api/v1/candles` |
| `get_trades` | 단일 종목 최근 체결. count 1~50. 체결강도·매수우위 판단 | READ | 단건 only | `/api/v1/trades` |
| `get_price_limits` | 당일 상/하한가. 분할매수 가격밴드 sanity-check | READ | PLAN이 가격범위 검증에 선행 호출 | `/api/v1/price-limits` |
| `get_stock_warnings` | 매수 유의(정리매매·단기과열·경고/위험·VI). plan_order가 자동 선행 | READ | 위험종목 경고 플래그 | `/api/v1/stocks/{symbol}/warnings` |
| `stock_info` `(symbols)` | 종목 마스터 다건(최대50, 콤마구분). 상장상태·securityType·ETF여부·상장/상폐일·발행주식수+합성 시가총액(발행주식수×현재가)·레버리지배수·국내 거래정지/정리매매 플래그(koreanMarketDetail). 주문 직전 거래가능 점검 | READ | 봉투 `{ok,data,_mock}`, market/currency 라벨, tradingSuspended 플래그 | `/api/v1/stocks` (+prices 합성) |

### 4.3 단건 READ — 시장정보
| 도구 | 설명 | RW | 안전장치 | 매핑 |
|---|---|---|---|---|
| `get_exchange_rate` | KRW↔USD 표시환율(1분). "50만원"→US orderAmount(USD) 환산. validUntil 노출 | READ | 계좌무관, 체결가 보장 아님 명시, preview TTL=min(120s,validUntil-now) | `/api/v1/exchange-rate` |
| `get_market_calendar` | KR(KRX·NXT)/US 장 운영·휴장일·세션(KST). region 통합 | READ | 계좌무관, NXT 세션 포함, PLAN 세션 적합성 검증 | `/market-calendar/KR · /US` |

### 4.4 단건 READ — 계좌/주문
| 도구 | 설명 | RW | 안전장치 | 매핑 |
|---|---|---|---|---|
| `get_account_info` | 본인 계좌 목록(accountNo/Seq/Type, 정상상태만). 다계좌 선택 | READ | accountSeq 내부 캐시·헤더용, 응답엔 index 매핑 | `/api/v1/accounts` |
| `get_holdings` | 보유종목 상세(수량·평단·평가·손익·일손익·cost+합산). overview의 raw 버전 | READ | accountSeq 래퍼 주입 | `/holdings` (+accounts 선행) |
| `get_buying_power` | 통화별 매수가능 금액(현금, 미수 제외). 사이징 상한 검증 | READ | accountSeq 주입, ORDER 예약쿼터 그룹 | `/buying-power` (+accounts) |
| `get_sellable_quantity` | 종목별 매도가능 수량. 매도 plan 검증 선행 | READ | accountSeq 주입 | `/sellable-quantity` (+accounts) |
| `get_commission_policy` | 시장별 수수료(국내 면제 2026.6 만료 가능→런타임 조회) | READ | 수수료 하드코딩 금지 | `/commissions` (+accounts) |
| `get_orders` | 주문 목록(OPEN/CLOSED). execution(filledQty/avgPrice/tax/settlement)·페이지네이션 | READ | accountSeq 주입, 10 OrderStatus·부분체결 노출 | `/api/v1/orders` |
| `track_order` | 단일 주문 추적. orderId/clientOrderId로 OrderStatus 10값·execution·settlementDate. reconcile·체인추적 진실원천 | READ | 둘 중 하나 필수, accountSeq 주입 | `/orders/{orderId}` (또는 필터) |
| `logout` | 캐시된 OAuth 토큰 폐기(메모리 + token.json 삭제). 다음 호출 시 재발급 | READ | secret 미접근(.env 유지)·토큰값 미출력, mock=no-op (inv 13A) | (네트워크 없음, 로컬 토큰 무효화) |

### 4.4A 합성/분석 READ — alpha (analytics.py · 공통 봉투 `{ok,data,_mock}`)
> intraday 마이크로구조군 + stock-dashboard 알파 이식군 + 실계좌 진단군 + what-if. 모두 부작용0(`mutates:false`), 단순 래퍼가 아닌 합성 고수준 도구. 반환은 공통 봉투 `{ok:true,data,_mock}`/`{ok:false,error}`.
| 도구 | 설명 | RW | 안전장치 | 매핑 |
|---|---|---|---|---|
| `orderbook_pressure` `(symbol, top_n=5)` | 호가 매수/매도 불균형비·스프레드·상위 N호가 벽·체결강도. 단기 수급 압력 → 단타 진입 판단 | READ | 봉투 `{ok,data,_mock}`, 단건, 폴링 금지 | `/orderbook` + `/trades` (합성·analytics) |
| `intraday_vwap` `(symbol, aggregate_5m=true)` | 당일 VWAP·vwapGap(%)·당일 레인지 내 위치. "지금 VWAP 위야 아래야?" 1분봉을 5분봉 집계 | READ | 봉투, 1m 캔들+현재가 보강, 단건 | `/candles(1m)` + `/prices` (합성) |
| `recent_trades_tape` `(symbol, count=50, big_trade_mult=3.0)` | 최근 체결 테이프: 체결강도·대량체결 표식·틱 흐름. 호가와 짝 | READ | 봉투, 단건 only | `/trades` (analytics) |
| `price_limit_proximity` `(symbol)` | 상/하한가 근접도(%)·잔여폭. 상따 모니터링(KRX 전용 라벨, US 참고) | READ | 봉투, KRX 라벨·US 참고 구분, 단건 | `/price-limits` + `/prices` (합성) |
| `scalp_score` `(symbols, interval="1d")` | 단/다건 단타 적합도 0~100·S~D 등급+사유. "지금 단타 칠 만한 종목?" 일봉+현재가+체결 기반 | READ | 봉투, 다건(최대50) 미해석 종목 skip, on-demand | `/candles`+`/prices`+`/trades`→indicators(scalpScore) |
| `supply_demand_flow` `(symbol)` | 외국인/기관/개인 순매수 억원·추세. "외국인이 사고 있어?" | READ | 봉투, **토스 미제공**→mock=합성(`_source=mock`)/live=보조소스 없으면 null(추측 금지) | (토스 외부) stock-dashboard(Naver) 보조 소스 |
| `rank_watchlist` `(symbols=None, top_n=5, sort_by="scalpScore", interval="1d")` | 워치리스트 TOP N 단타 후보 + 한 줄 브리핑. "오늘 뭐 봐야 해?" 대표 훅. sort_by=scalpScore\|changePct\|volValue\|rsi\|volRatio | READ | 봉투, symbols 미지정 시 mock watchlist 상위, 최대30 batch | `/candles`+`/prices`(N) → indicators 랭킹 |
| `portfolio_risk_xray` `(account_index=0, max_single_weight=0.30)` | 집중도(허핀달)·단일종목 최대비중·시장/통화/현금 비중·위험 플래그. "내 포트 한쪽 쏠렸어?" | READ | 봉투, accountSeq 주입, fx 환산, 임계초과 플래그 | `/holdings`+`/buying-power`+`/exchange-rate` (합성) |
| `pnl_attribution` `(account_index=0, top_n=5)` | 종목별 손익 기여도 TOP·손익 귀속 요약. 잔고 뷰어를 스토리텔링으로 | READ | 봉투, accountSeq 주입, fx 환산 | `/holdings` (+exchange-rate) (analytics) |
| `impact_of_order` `(symbol, side, quantity, price=None, currency=None, account_index=0)` | what-if: 가상 주문이 집중도·현금비중·통화·손익을 어떻게 바꾸는지. **실행 없음**. 표준 체인 `analyze_symbol→impact_of_order→preview_order→place_order`의 2단계 | READ/what-if | 봉투, **POST 없음**, 통화/시장 정합 위반=`CURRENCY_MISMATCH` 거부, price 미지정 시 현재가 | `/holdings`+`/buying-power`+`/prices` (합성·미실행) |

### 4.5 PLAN (dry-run, 주문 미생성)
| 도구 | 설명 | RW | 안전장치 | 매핑 |
|---|---|---|---|---|
| `plan_order` | 단일 주문 dry-run. 현재가·tickSize·세션·매수가능액/매도가능수량·수수료·세금추정·고가확인 검증. US 소수점절삭·KR 호가단위 검증. 동결 스냅샷 + preview_token 발급 | PLAN | **주문 미생성**, 위반 시 토큰 미발급+사유, 1억↑=high_value, market/currency/orderType 교차검증, confirm_phrase 동봉 | 검증용 READ 조합, POST 미호출 |
| `plan_split_order` | "엔비디아 50만원 3분할 매수" 자연어→N슬라이스 분해, 각 plan_order 검증 후 합산비용·환산·세션 묶은 배치 preview_token | PLAN | **주문 미생성**, 합산 1억↑=high_value, 매수가능액 초과 시 미발급, KRW→US 환산내역+경고, TTL=min(120s,fx validUntil) | resolve_symbol+exchange-rate+plan_order(N회) |
| `plan_cancel` | 단일 미체결 주문 취소 미리보기. get_order로 상태 선검증 후 단건 preview_token 발급. cancel_order_confirmed로만 실행 | PLAN | **미실행**, FILLED/CANCELED/PENDING_* 차단, orderId 동결 스냅샷, confirm_phrase 동봉 | `/orders/{orderId}` 조회, POST 미호출 |
| `plan_cancel_all` | 미체결(OPEN) 일괄취소 미리보기. get_orders(OPEN)로 대상·영향 산출, 배치 preview_token | PLAN | **미실행**, 대상 orderId·status 동결 스냅샷, FILLED/이미취소 제외 | `/orders(status=OPEN)`, POST 미호출 |

### 4.5A 계산 전용 PLAN — alpha (POST·preview_token 미발급, 봉투 `{ok,data,_mock}`)
> dry-run 플랜군. 주문도 preview_token도 만들지 않는 **순수 계산** 도구. 트리거 도달/실행은 `plan_order`로 분리. 모두 `executed:false`+`_disclaimer`(참고용 플랜) 명시.
| 도구 | 설명 | RW | 안전장치 | 매핑 |
|---|---|---|---|---|
| `plan_dca` `(symbol, period_amount, periods, budget_currency="KRW", interval="1d")` | 적립식(정액) DCA 스케줄 시뮬. "월 50만원 적립하면?" 과거 캔들 종가로 회차별 수량·누적 평단 시뮬(periods≤120) | PLAN/계산 | **POST·token 없음**, 통화/시장 정합(불변식12) 검증, `executed:false`+disclaimer | `/candles`+`/exchange-rate` (계산만) |
| `plan_trailing_stop` `(symbol, entry_price=None, trail_pct=5.0, quantity=None)` | 트레일링스탑 플랜: 진입가·추적%·현재 트리거가·여유폭·미실현%. 자동추적/실행 없음, 트리거 도달 시 plan_order로 수동 진행 | PLAN/계산 | **POST·token 없음**, trail_pct 0~100 검증, KRW 트리거 tickSize 정렬, `executed:false`+disclaimer | `/prices` (계산만) |
| `plan_rebalance` `(target_weights, account_index=0, total_budget_krw=None)` | 목표 비중("심볼:비중,…") 대비 매수/매도 수량 플랜. 현재 보유 평가액 대비 차이를 수량 환산. 실행은 plan_order로 분리 | PLAN/계산 | **POST·token 없음**, accountSeq 주입, 비중합 1.0 근처 검증, `executed:false`+disclaimer | `/holdings`+`/prices`+`/exchange-rate` (계산만) |

### 4.6 EXECUTE (실행 — `TOSS_ALLOW_LIVE_ORDERS=1`일 때만 등록)
| 도구 | 설명 | RW | 안전장치 | 매핑 |
|---|---|---|---|---|
| `place_order_confirmed` | 실주문 생성. **새 인자 안 받고 preview_token으로만** 동작. confirm_phrase 재입력 + live + 매 POST KILL 재확인 3중. clientOrderId 멱등(10분). POST 후 Reconciler GET 확정(재POST 금지), 409=성공 | EXECUTE | opt-in 등록, token 없음/만료/소비/슬리피지초과=거부, confirm_phrase 누락=dry-run 강등, KILL=1 무조건 거부, 429 백오프 | `POST /api/v1/orders` |
| `modify_order_confirmed` | 미체결 정정(가격/수량). preview_token + confirm_phrase + live/KILL. FILLED/CANCELED/PENDING_* 차단. 새 orderId→추적 교체 | EXECUTE | opt-in, 원주문 상태 선검증, 새 orderId 추적 갱신, KILL 거부·409=성공·reconcile · ⚠️**현재 plan_modify 미구현으로 도달 불가**(공식 modify body 스키마 확보 후 배선 예정) | `POST /orders/{orderId}/modify` |
| `cancel_order_confirmed` | 단건 주문 취소(plan_cancel 토큰 소비). preview_token + confirm_phrase + live/KILL. 새 orderId→추적 교체. CANCEL_REJECTED 별도 처리 | EXECUTE | opt-in, terminal/in-transition 차단, `cancel:{orderId}` 멱등(중복취소 차단), KILL 거부·409=성공 | `POST /orders/{orderId}/cancel` |
| `cancel_all_confirmed` | 미체결 일괄취소. plan_cancel_all 배치 token + confirm_phrase + live/KILL. 결정적 멱등키로 순차 취소, 결과 집계. 부분 실패 허용 | EXECUTE | opt-in, 토큰 스냅샷 외 미접근, 건별 reconcile·409=성공, ORDER 예약쿼터 | `POST /orders/{orderId}/cancel` (N건) |

---

## 5. 아키텍처

### 5.1 파일 구성 (`toss-trader/`)
```
server_mcp.py        L1 FastMCP(stdio) 진입점. read/plan 항상 등록.
                     write 도구는 TOSS_ALLOW_LIVE_ORDERS=1일 때만. 시작 시 _mode 확정·로그.
backend_factory.py   모드 1회 확정: live = (TOSS_LIVE=='1') AND (secret 유효) AND (TOSS_KILL!='1')
                     아니면 mock. 프로세스 수명 동안 불변. backend.mode 속성.
toss_client.py       L2 단일 래퍼 (모든 도구가 경유):
  ├ TokenManager     POST /oauth2/token(form-urlencoded), 401시 Basic 폴백, expires_in 캐싱,
  │                  single-flight 락 + token.json 파일락(0600), 토큰 로그 금지
  ├ AccountContext   GET /accounts → accountSeq 캐시 → X-Tossinvest-Account 자동 주입
  ├ RateGuard        그룹별 토큰버킷(AUTH/MARKET_DATA/CHART/STOCK/INFO/ACCOUNT/ASSET/ORDER/HISTORY),
  │                  X-RateLimit-*/Retry-After 존중, 429 백오프, ORDER+사전검증 예약 쿼터
  ├ EnvelopeParser   BFF ApiResponse(result/error) 파싱, 가격 문자열·ISO8601 보존, OPG등 unknown enum 허용
  └ Reconciler       POST 후 불확실 시 GET으로 접수 확정. 재POST 절대 금지.
error_mapper.py      ApiError.code(flat string)→사용자 메시지 + data 힌트. unknown code 허용.
                     제출시점 에러(opposite-pending/prerequisite-required/restricted/maintenance/
                     request-in-progress) 분류.
redaction.py         로깅 필터: secret/token/Authorization/eyJ(JWT) 마스킹. 모든 logger 부착.
preview_store.py     PLAN preview_token 스토어. 동결 스냅샷. TTL=min(120s, fx validUntil-now).
                     1회 소비. orders 활성 시 0600 파일 백업.
indicators.py        RSI·이평선(20/60/120)·단타점수·체결강도(candles/trades 기반).
                     외국인수급은 토스 미제공→stock-dashboard 네이버 소스 별도 표기.
backends/mock.py     MockBackend: socket 차단. fixtures/ 실스키마 반환.
                     시세 시드 = ~/stock-dashboard/watchlist.json 200종목. _mode='mock' + MOCK 프리픽스.
backends/live.py     LiveBackend: 실 네트워크. 동일 응답형 → 도구 코드 0 변경.
models/              datamodel-code-generator로 spec v1.1.1에서 생성한 Pydantic 모델(드리프트 감시).
spec/toss_openapi.json  동결 ground-truth(v1.1.1, 20 path). 레포 커밋.
fixtures/*.json      mock fixture (non-FILLED·부분체결·22에러·휴장일·다계좌 케이스 포함).
README.md            등록 절차 + BYOK/약관 안내.
.env                 client_id/secret (서버 env only, 0600, .gitignore). token.json·preview.json도 .gitignore.
```

### 5.2 환경변수
| 변수 | 기본값 | 의미 |
|---|---|---|
| `TOSS_CLIENT_ID` | (unset) | BYOK client_id (env only) |
| `TOSS_CLIENT_SECRET` | (unset) | BYOK secret (env only, 응답/로그/스키마 절대 비노출) |
| `TOSS_LIVE` | unset(=mock) | `1`이고 secret 유효해야만 live |
| `TOSS_ALLOW_LIVE_ORDERS` | unset(=0) | `1`일 때만 write 도구 등록 (1차 출시 조회전용) |
| `TOSS_KILL` | unset | `1`이면 글로벌 킬스위치 — mock 강등 + 모든 POST 거부(매 POST 직전 재확인) |
| `TOSS_ACCOUNT_INDEX` | 0 | 기본 계좌 인덱스 |
| `TOSS_DATA_DIR` | `~/.toss-trader` | token.json/preview.json 저장 위치(0600) |
| `STOCK_DASHBOARD_DIR` | `~/stock-dashboard` | watchlist 시드 경로(재사용) |

### 5.3 안전 불변식
1. read/plan = `mutates:false`, execute만 `true`.
2. 기본 mock. live는 명시 opt-in.
3. write 도구는 `TOSS_ALLOW_LIVE_ORDERS=1` 시에만 등록.
4. 실주문 = `preview_token` + `confirm_phrase` + `TOSS_LIVE` 3중 + 매 POST `TOSS_KILL` 재확인.
5. clientOrderId 결정적(의도해시) · POST 후 reconcile · 409=성공.
6. modify/cancel 새 orderId로 추적 교체.
7. client_secret/token redaction · 파일 0600.
8. RateGuard 헤더 기반 · ORDER 예약쿼터(폴링이 주문을 굶기지 않게).

---

## 6. 보안 모델

> 두 차례 비평(보안 14건 + 완결성 5건)을 spec v1.1.1로 재검증해 반영.

### 6.1 자격증명 보호
- `client_secret`은 **서버 프로세스 env에만** 존재. 응답/로그/스키마/fixture 어디에도 비노출.
- `redaction.py`가 모든 logger에 부착되어 `client_secret`/`access_token`/`Authorization`/`eyJ`(JWT) 패턴 마스킹.
- fixture는 **합성값만** + `eyJ` 스캔으로 실토큰 혼입 차단.
- `token.json`/`preview.json`/`.env` 모두 `0600` 권한 + `.gitignore`.
- MCP 전용 `client_id` 사용(대시보드와 분리) → 동일 client_id 충돌로 인한 401 storm 방지.

### 6.2 주문 안전 (HIGH 비평 대응)
| 위험 | 대응 |
|---|---|
| 주문 POST가 500/타임아웃인데 실제 접수됨 → 중복주문 | 결정적 `clientOrderId`(의도해시 base32 ~28자) + POST 후 **무조건 GET reconcile**. 응답 불확실 시 **절대 재POST 금지** |
| preview 120s vs 멱등 10분 불일치로 409 | clientOrderId를 preview 의도해시에서 파생(시간 무관). **409 Conflict = 이미 접수 = 성공** 처리 |
| confirm bool이 자동 true로 채워짐 | `confirm_phrase` 재입력 요구(예: 종목/수량 문구). 누락 시 dry-run 강등 |
| mock/live 혼동 | 단일 팩토리가 프로세스당 1회 모드 확정. 모든 응답에 `_mode` 필드, mock 응답에 `MOCK` 프리픽스, mock에서 socket 차단 |
| KILL/ENABLE 캐시 우회 | 모든 POST 직전 env 재확인(KILL atop every POST) |
| modify/cancel 새 orderId로 체인 stale | 반환된 새 orderId로 추적객체 교체 + GET 재조회. FILLED/CANCELED/PENDING_*는 정정/취소 차단 |

### 6.3 Rate-limit
- 그룹별 토큰버킷 + ORDER/사전검증(buying-power/sellable-quantity)용 **예약 쿼터**(폴링이 주문을 굶기지 않게).
- `X-RateLimit-Limit/Remaining/Reset`·`Retry-After` 런타임 파싱·존중, 429 지수백오프.

### 6.4 에러 처리
- `error_mapper.py`: flat string code → 사용자 메시지 + `data` 힌트(nearestPrices/tickSize/limits) 추출. **unknown code 허용(원문 노출)**.
- 제출시점 에러(`opposite-pending`/`prerequisite-required`/`restricted`/`maintenance`/`request-in-progress`)는 reconcile 흐름에서 처리.

### 6.5 출시 게이트
결정적 clientOrderId · reconcile · 409=성공 · redaction · 단일 팩토리 통과 전까지 **`TOSS_ALLOW_LIVE_ORDERS=0`** 유지.

---

## 7. Mock 모드

- `backend_factory`가 프로세스 시작 시 1회 모드 확정. 키 없음 또는 `TOSS_LIVE!=1` 또는 `TOSS_KILL=1` → `MockBackend`.
- **MockBackend는 socket을 차단**해 실수로 네트워크 호출이 나가지 않게 함.
- fixture는 실스키마 그대로: BFF envelope · 문자열 가격 · ISO8601 · integer accountSeq · opaque orderId(가변길이) · oneOf 주문 · OrderStatus 10상태 · 부분체결 · tax/settlementDate · 22 에러 케이스 · 휴장일 · 다계좌.
- 시세 시드 = `~/stock-dashboard/watchlist.json` 200종목(code/name/market).
- 모든 mock 응답에 `_mode='mock'` + `MOCK` 프리픽스.
- mock에서도 `X-Tossinvest-Account` 흐름(accounts→accountSeq→주입) · oneOf 주문검증 · tickSize/세션/잔고 위반 · high-value(1억↑) · OrderStatus 전이를 동일 모사 → 실키 전환 안전.
- 도구는 backend 인터페이스만 호출하므로 mock/live 분기 코드 0. **live 전환은 env만 바꾸면 됨.**

---

## 8. 로드맵 (단계적 개방)

| 단계 | 게이트 env | 등록 도구 | 동작 |
|---|---|---|---|
| **0. mock 조회** (현재) | (기본) | READ + PLAN | 네트워크 차단, fixture 응답. 자연어 인터랙션 전체 검증 가능 |
| **1. live 조회** | `TOSS_LIVE=1` + 유효 secret | READ + PLAN | 실계좌/실시세 조회. dry-run(plan)도 실데이터로 검증. 주문 POST 불가(write 미등록) |
| **2. dry-run 실데이터** | 위 + 검증 통과 | READ + PLAN | plan_order/plan_split_order가 실 tickSize·세션·잔고로 preview_token 발급. POST는 여전히 없음 |
| **3. 실주문** | 위 + `TOSS_ALLOW_LIVE_ORDERS=1` | READ + PLAN + EXECUTE | place/modify/cancel/cancel_all 등록. preview_token+confirm_phrase+live+KILL 4중 게이트 |

**3단계 진입 전 통과 게이트**: 결정적 clientOrderId · POST 후 reconcile · 409=성공 처리 · redaction 필터 · 단일 팩토리(mock/live 1회 확정) — 5개 모두 검증 완료.

---

## 9. 미검증 항목 (키 발급 후 반드시 확인)

> 사전 조사·spec 파싱 기반 추정이며, 실키 발급 후 live로 검증해 정정해야 하는 항목.

1. **`X-Tossinvest-Account` 헤더명·값 형식** — 계좌 호출에 필요하다고 추정. 실제 헤더 키 이름(대소문자/하이픈), 값이 `accountSeq`(integer)인지 `accountNo`(문자열)인지 live로 확정.
2. **`expires_in` 실제 값** — ~3600s 추정. 동적 캐싱하므로 기능엔 영향 없으나 선제 갱신 임계(10%) 적정성 확인.
3. **토큰 발급 인증 방식** — form-urlencoded body 우선, 401 시 Basic 폴백으로 설계. 어느 쪽이 정식인지 확정(불필요한 폴백 제거).
4. **client당 1토큰 규칙** — 재발급 시 이전 토큰 무효화 여부. dashboard와 client_id 분리 전제의 실제 충돌 동작 확인.
5. **Rate-limit 실제 한도** — `X-RateLimit-Limit` 그룹별 실수치, 윈도우, ORDER 예약쿼터 적정 배분.
6. **`OrderCreateRequest` oneOf 분기** — 수량기반 vs US 금액기반(`orderAmount`)의 정확한 필수 필드 조합, US 소수점 절삭 규칙.
7. **tickSize(호가단위) 소스** — price-limits 응답에 tickSize가 오는지, 별도 종목 메타에 있는지. PLAN 검증의 정확도 핵심.
8. **high-value(1억↑) 임계 트리거** — `confirm-high-value-required` 에러가 실제로 1억 기준인지, 통화별(KRW/USD) 기준인지.
9. **modify/cancel 새 orderId 반환 동작** — 실제로 매번 새 orderId가 발급되는지, 원주문 추적의 정확한 체인 규칙.
10. **`commissions` 응답 구조 + 국내 면제 2026.6 이후** — 면제정책 만료 후 실제 수수료가 응답에 어떻게 반영되는지(런타임 조회로 대응하나 필드 확인 필요).
11. **NXT 세션 데이터** — market-calendar/KR가 NXT 세션을 실제로 분리 노출하는지.
12. **외국인수급** — 토스 Open API 미제공 확정(현재 stock-dashboard 네이버 소스로 분리 표기). 추후 제공 여부 모니터링.
13. **WebSocket** — 미공개로 REST 폴링(최대 1초)으로 근사. 실시간 스트림 제공 시 재설계.
14. **`market-calendar`·`exchange-rate` `validUntil`** — exchange-rate의 validUntil 필드 존재·형식 확인(preview TTL 클램프 정확도).
