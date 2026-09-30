# toss-trader — 내 돈을 아는 AI 트레이더 (Claude × 토스증권 Open API MCP)

자연어로 Claude에게 말하면, 토스증권 실계좌·실시세에 연결해 조회하고, 주문을 플랜→검증→확정 2단계로 실행한다.

> "내 계좌 어때?" · "엔비디아 분석해줘" · "엔비디아 50만원 3분할 매수 플랜 짜줘"

`Python 3.9+` · `MCP (FastMCP)` · `토스증권 Open API v1.2` · **304 유닛테스트** · **mock 기본 · 실돈 안전설계**

---

## 한눈에

LLM이 **실제 증권 계좌를 자연어로 다루게** 하는 MCP 서버 + 그 위에서 도는 **변동성 적응형 스윙 트레이딩 엔진**이다. 핵심은 "AI가 실돈을 만진다"는 전제를 정면으로 받아, **안전을 1순위 제약**으로 놓고 전체를 설계한 것.

- **21개 합성 고수준 도구** — 얇은 API 래퍼가 아니라, LLM이 한 번의 호출로 판단할 수 있게 시세·캔들·지표·유의사항을 합성.
- **실돈 안전 13대 불변식** — mock 기본 · 단계적 해제 · 이중 게이트 실주문 · 킬스위치 · 멱등 재조정 · secret redaction.
- **키 없이도 완전 동작** — mock 모드가 실스키마(부분체결·세금·결제일·휴장·다계좌·에러)를 그대로 모사.

## 왜 이렇게 설계했나 (설계 철학)

이 프로젝트에서 내가 내린 판단들:

1. **"실돈이 오간다 → 안전이 기능보다 우선이다."**
   AI가 자동으로 주문을 낼 수 있는 순간, 가장 큰 리스크는 시장이 아니라 **오작동·연쇄호출·유령체결**이라고 봤다. 그래서 기본을 mock으로 두고, **mock → 조회 live → 실주문**을 환경변수로 한 단계씩만 열게 했다. 실주문은 새 인자를 못 받고, 반드시 `plan_*`이 발급한 **preview_token + 재입력 confirm_phrase**를 거쳐야 한다(4중 게이트). 킬스위치 하나로 즉시 전량 mock 강등.

2. **"LLM은 얇은 래퍼로는 못 쓴다 → 도구를 합성하라."**
   `get_price`·`get_candles`를 각각 주면 LLM이 여러 번 헤매며 호출한다. 그래서 `analyze_symbol`(시세+캔들+RSI/이평/단타점수/체결강도+매수유의 1콜), `portfolio_overview`(계좌+보유+매수가능+수수료 1콜)처럼 **판단 단위로 합성**했다. 읽기 20 : 쓰기 1 비율을 유지해 위험한 도구를 최소화.

3. **"감이 아니라 규칙으로 — 그리고 변동성에 적응하게."**
   포지션 크기를 달러로 균등하면 고변동주가 리스크를 지배한다. 그래서 **리스크 균등 사이징**(슬롯당 목표 ATR 리스크를 고정, 크기는 변동성에 반비례)과 **k×ATR 목표가**(저변동주엔 작은·도달가능 목표, 고변동주엔 큰 목표)로 바꿨다. 체결은 **저널을 원장으로 삼아 FIFO 손익**을 재구성하고, 청산 우선순위(실적→손절→추세이탈→본전방어→목표→트레일링)를 코드로 못박았다.

4. **"만들고 끝이 아니라, 검증이 자산이다."**
   실돈 주문경로는 **적대적 안전검수**(별도 리뷰어가 반증 우선으로 뜯어봄)를 통과해야 머지했고, 회귀는 **304개 유닛테스트**로 잠갔다. 스펙은 [`SPEC.md`](./SPEC.md)를 단일 진실원천으로 두고 코드보다 먼저 합의.

## 아키텍처

```
자연어 (Claude)
   │  MCP (FastMCP)
   ▼
server_mcp.py ── 21개 도구 등록 (mode gate: 실주문은 ALLOW_LIVE_ORDERS=1일 때만)
   │
   ├─ config.py        BYOK 자격증명·모드 판정(mock/live)·secret redaction
   ├─ toss_client.py   OAuth2 토큰 캐시·단일플라이트 갱신·rate 백오프·에러 정규화
   ├─ safety.py        실주문 가드(preview_token·confirm_phrase·kill-switch·멱등)
   ├─ indicators.py    RSI·이평·ATR·체결강도 등 지표
   ├─ stock_screener.py  유니버스 스크리닝·RS 랭킹·매수존 판정
   ├─ index_engine.py    레짐(200일선)·추세 판정
   └─ execute.py       스윙 실행기: 리스크 사이징·k×ATR 목표·저널 원장·청산 판정·30분 크론
```

## 무엇을 보여주는가 (포트폴리오 관점)

- **실돈 제약 하의 외부 API 통합** — OAuth2, rate-limit, 부분체결/재조정, 시간외·휴장 등 실거래 엣지케이스 처리.
- **LLM 도구 설계** — 자연어 의도 → 합성 도구 → 안전한 실행까지의 인터페이스 설계.
- **정량 전략 엔지니어링** — 변동성 적응 사이징·백테스트·저널 기반 손익귀속.
- **안전공학·검증 규율** — 이중 게이트·킬스위치·감사로그·적대적 리뷰·304 테스트.

> ⚠️ 이 저장소는 **엔지니어링·설계**를 보여주기 위한 것이다. 수익을 보장하지 않으며, 모든 매매 결과는 계좌주 책임이다(아래 면책 참조).

---

## ⚠️ 먼저 읽을 것 — 보안 경고 & 면책

- **`client_secret`은 절대 클라이언트/채팅/로그에 노출하지 마라.** 서버(MCP 프로세스) 환경변수에만 둔다. 이 서버는 secret/토큰을 응답·로그·스키마 어디에도 출력하지 않도록 redaction을 건다.
- **실주문은 전적으로 본인 책임이다.** 이 도구가 생성하는 주문 플랜·실행은 시장 상황·체결가·세금·수수료에 따라 손실로 이어질 수 있다. 표시환율·예상 체결가는 **체결을 보장하지 않는다.** 모든 매매의 결과(체결·미체결·손익·세금)는 **계좌주 본인의 책임**이며, 제작자/Claude/Anthropic은 책임지지 않는다.
- **이 서버는 개인 전용이다.** 토스 Open API 약관(개인전용·재배포 금지·자동매매 책임은 계좌주)에 동의한 범위에서만 사용한다. 약관: https://home.tossinvest.com/ko/terms/v2?id=752
- **단계적으로 열어라.** 처음엔 mock → 조회만 live → 마지막에만 실주문. 실주문 도구는 `TOSS_ALLOW_LIVE_ORDERS=1`을 명시할 때만 등록된다.
- 글로벌 킬스위치 `TOSS_KILL=1`을 켜면 즉시 mock으로 강등되고 모든 주문 POST가 거부된다.

---

## 1. 설치

요구사항: Python 3.9+

```bash
# 저장소(또는 toss-trader/ 디렉토리)로 이동
cd ~/toss-mcp

# 가상환경 권장
python3 -m venv .venv
source .venv/bin/activate

# 의존성 설치
pip install -r requirements.txt
# (requirements: mcp[cli] / fastmcp, httpx, pydantic)
```

> mock 모드로만 쓸 거라면 키 없이 바로 다음 단계(§4 등록)로 가도 된다.

---

## 2. 토스증권 Open API 키 발급/신청

1. 토스증권 Open API 안내 페이지 접속: **https://corp.tossinvest.com/ko/open-api**
2. 약관 동의: **https://home.tossinvest.com/ko/terms/v2?id=752** (개인 전용·재배포 금지·자동매매 책임은 계좌주)
3. 신청·심사 후 **`client_id` / `client_secret`** 발급.
4. (참고) 공식 OpenAPI 스펙: **https://openapi.tossinvest.com/openapi-docs/latest/openapi.json**
   비공식 선행 CLI 참고: https://github.com/JungHoonGhae/tossinvest-cli
5. 발급받은 키는 이 MCP 전용으로 쓴다(stock-dashboard 등 다른 도구와 **다른 client_id 권장** — 동일 키 충돌 시 토큰 무효화로 401이 번갈아 터질 수 있음).

> **키 발급 전이라도 이 서버는 mock으로 완전히 동작한다.** 키는 실계좌·실시세를 붙일 때만 필요하다.

---

## 3. `.env` 설정

`toss-trader/.env` 파일을 만들고 `0600` 권한으로 둔다. **반드시 `.gitignore`에 포함.**

```dotenv
# --- BYOK 자격증명 (서버 env only, 절대 커밋 금지) ---
TOSS_CLIENT_ID=your_client_id
TOSS_CLIENT_SECRET=your_client_secret

# --- 모드 게이트 ---
TOSS_LIVE=0            # 0/unset = mock(기본). 1 + 유효 secret 이어야 live
TOSS_ALLOW_LIVE_ORDERS=0   # 0/unset = 조회 전용(write 도구 미등록). 1이어야 실주문 도구 등록
# TOSS_KILL=1          # 켜면 즉시 mock 강등 + 모든 주문 POST 거부 (긴급 정지)

# --- 선택 ---
TOSS_ACCOUNT_INDEX=0           # 기본 계좌 인덱스 (다계좌 시)
TOSS_DATA_DIR=~/.toss-trader   # token.json/preview.json 저장(0600)
STOCK_DASHBOARD_DIR=~/stock-dashboard  # mock 시세 시드용 watchlist 경로
```

```bash
chmod 600 ~/toss-mcp/.env
```

권장 진행 순서:
1. 처음: `TOSS_LIVE=0`, `TOSS_ALLOW_LIVE_ORDERS=0` → **mock 조회**로 전체 흐름 검증.
2. 키 발급 후: `TOSS_LIVE=1` → **live 조회 + 실데이터 dry-run**. 주문은 아직 안 됨.
3. 충분히 검증되면: `TOSS_ALLOW_LIVE_ORDERS=1` → **실주문** 도구 등록.

---

## 4. Claude에 MCP 등록

### A) Claude Code (CLI) — 가장 간단

```bash
claude mcp add toss-trader -- python3.12 ~/toss-mcp/server_mcp.py
```

환경변수는 `.env`에서 읽거나, 등록 시 `-e`로 주입한다:

```bash
claude mcp add toss-trader \
  -e TOSS_CLIENT_ID=your_client_id \
  -e TOSS_CLIENT_SECRET=your_client_secret \
  -e TOSS_LIVE=0 \
  -e TOSS_ALLOW_LIVE_ORDERS=0 \
  -- python3.12 ~/toss-mcp/server_mcp.py
```

### B) 프로젝트 `.mcp.json`

```json
{
  "mcpServers": {
    "toss-trader": {
      "command": "python3.12",
      "args": ["~/toss-mcp/server_mcp.py"],
      "env": {
        "TOSS_CLIENT_ID": "your_client_id",
        "TOSS_CLIENT_SECRET": "your_client_secret",
        "TOSS_LIVE": "0",
        "TOSS_ALLOW_LIVE_ORDERS": "0"
      }
    }
  }
}
```

### C) Claude Desktop — `claude_desktop_config.json`

경로: macOS `~/Library/Application Support/Claude/claude_desktop_config.json` · Windows `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "toss-trader": {
      "command": "python3.12",
      "args": ["~/toss-mcp/server_mcp.py"],
      "env": {
        "TOSS_CLIENT_ID": "your_client_id",
        "TOSS_CLIENT_SECRET": "your_client_secret",
        "TOSS_LIVE": "0",
        "TOSS_ALLOW_LIVE_ORDERS": "0"
      }
    }
  }
}
```

등록 후 Claude Desktop을 재시작한다. (경로는 본인 환경의 절대경로로 교체. 이 머신은 ~/toss-mcp)

---

## 5. mock 모드로 먼저 써보기

`TOSS_LIVE=0`(기본)이면 서버는 **네트워크를 차단하고 fixture로 응답**한다. 모든 mock 응답엔 `_mode: "mock"` + `MOCK` 프리픽스가 붙는다.

체험 흐름:
1. **"내 계좌 어때?"** → 가상 포트폴리오(평가/매입/총손익/일손익) 카드.
2. **"삼성전자 분석해줘"** → RSI·이평선(20/60/120)·단타점수·체결강도 + 매수 유의.
3. **"엔비디아 50만원 3분할 매수 플랜 짜줘"** → 3슬라이스 dry-run + 합산 예상비용 + 환율 환산 + `preview_token`(주문은 생성 안 됨).

mock은 실스키마(부분체결·세금·결제일·휴장일·다계좌·에러 케이스)를 그대로 모사하므로, **여기서 검증한 대화 흐름은 live에서도 동일하게 동작**한다.

---

## 6. 자연어 사용 예시

| 말하면 | Claude가 하는 일 (내부 도구) |
|---|---|
| "내 계좌 어때?" | `portfolio_overview` — 계좌요약·보유종목·매수가능액·수수료 1콜 합성 |
| "엔비디아 분석해줘" | `analyze_symbol` — 시세+캔들+유의+지표(RSI/이평/단타점수/체결강도) 합성 |
| "삼성전자 지금 얼마야?" | `get_quote` — 현재가+전일대비+변동률+거래량(candles 보강) |
| "관심종목 다 현재가 보여줘" | `get_prices` — watchlist 200종목 1콜 |
| "엔비디아 호가창" | `get_orderbook` |
| "지금 미국장 열려?" | `get_market_calendar` (region=US) |
| "엔비디아 50만원 3분할 매수 플랜 짜줘" | `plan_split_order` — 자연어→3슬라이스 dry-run + `preview_token` (주문 미생성) |
| "삼성전자 7만원에 10주 사는 거 미리보기" | `plan_order` — tickSize/세션/잔고/수수료/세금 검증 + `preview_token` |
| "내 미체결 주문 다 취소 플랜" | `plan_cancel_all` — OPEN 주문 대상 산출 + 배치 `preview_token` |
| "아까 그 주문 어떻게 됐어?" | `track_order` — OrderStatus 10값·부분체결·결제일 |

### 실주문은 어떻게? (`TOSS_ALLOW_LIVE_ORDERS=1`일 때만)
실주문은 **새 주문 인자를 받지 않는다.** 반드시:
1. 먼저 `plan_*`로 **preview_token**과 **confirm_phrase(재입력 문구)**를 받는다.
2. `place_order_confirmed(preview_token, confirm_phrase)`로 실행.
3. 게이트 4중: `preview_token` 유효 + `confirm_phrase` 일치 + `TOSS_LIVE=1` + 매 POST 직전 `TOSS_KILL` 재확인.
4. POST 후 자동 reconcile(GET으로 접수 확정, 재POST 금지), `409=이미 접수=성공` 처리.

> confirm_phrase를 빼먹으면 실행되지 않고 dry-run으로 강등된다. 이것은 의도된 안전장치다.

---

## 7. 긴급 정지

```bash
# .env 또는 등록 env에서
TOSS_KILL=1
```
→ 모드가 즉시 mock으로 강등되고, 모든 주문 POST가 거부된다(매 POST 직전 재확인하므로 캐시 우회 불가).

---

## 8. 문제 해결

| 증상 | 원인/조치 |
|---|---|
| 항상 `MOCK` 응답만 나옴 | `TOSS_LIVE=1` + 유효 `TOSS_CLIENT_SECRET` 확인. `TOSS_KILL`이 켜져 있지 않은지 확인 |
| 실주문 도구가 안 보임 | `TOSS_ALLOW_LIVE_ORDERS=1` 설정 후 Claude/MCP 재시작(write 도구는 이때만 등록됨) |
| 401이 번갈아 터짐 | dashboard 등과 **client_id 공유** 의심 → MCP 전용 키로 분리 |
| 429 / rate-limit | 정상 백오프 동작. 관심종목 폴링은 `get_prices` 다건으로 묶어라(단건 반복 금지) |
| 계좌 조회 401/누락 | `X-Tossinvest-Account`(계좌 컨텍스트) 이슈 — 키 발급 후 실제 헤더 형식 확인 필요(SPEC §9) |

자세한 설계·엔드포인트·보안 모델·미검증 항목은 [`SPEC.md`](./SPEC.md) 참조.
