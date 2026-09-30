# RECOVERY.md — 재해복구 절차 (K10/G8)

실전(라이브) 트레이딩 시스템의 사고 대응 순서. **모든 복구의 첫 명령은 `--selftest`.**

```
PY=python3.12
cd ~/toss-mcp
```

## 0. 공통 진단
```
$PY execute.py --selftest      # 11+개 체크(설정/저널/HALT/백업무결성/토큰)
$PY execute.py --status        # 세션·레짐·서킷·백스톱·last_run
$PY execute.py --verify-broker # 원장 vs 브로커 대조(유령/미기록)
$PY execute.py --reconcile     # 미확정 주문 상태(READ-ONLY)
$PY execute.py --audit-join    # 감사로그↔저널 대사(READ-ONLY)
./ci.sh                        # 코드 무결성(컴파일+전체 테스트)
```

## 1. HALT가 걸려 있을 때 (매매 전면동결)
1. **사유 확인**: `cat ~/.toss-mcp/HALT` — 시각+사유가 기록돼 있음(무언 touch 아님).
2. 사유별 조치:
   - `체결확인 전부실패/불확실`: 토스 앱에서 해당 종목 주문내역·체결 확인 →
     체결됐으면 저널과 대조(`--verify-broker`), 다르면 수동 기록 검토.
     `--reconcile`로 브로커 상태 재확인.
   - `critical 저널기록 실패`: `--merge-fallback`으로 fallback 병합 → `--check-journal`.
   - `DUPLICATE인데 주문ID 없음`: 앱에서 중복주문 여부 확인 후 정리.
3. **모든 대조가 깨끗해진 후에만**: `rm ~/.toss-mcp/HALT` → `--selftest` 재확인.

## 2. 저널(원장) 손상/유실
1. `$PY execute.py --scan-journal` — 손상 라인 위치 확인(원본 무수정).
2. 백업 확인: `ls ~/.toss-trader/.backups/` (일일 백업, sha256 동반).
3. 무결성 검증: `shasum -a 256 <백업> `과 `.sha256` 파일 비교(또는 --selftest의 백업 무결성).
4. 복구: 손상 저널을 `.corrupt-<ts>`로 보존 이동 → 최신 건강한 백업을 복사 →
   백업 이후 체결분은 토스 앱 주문내역과 대조해 수동 반영 → `--check-journal` → `--verify-broker`.

## 3. fallback 파일이 있다는 경고(--status)
`$PY execute.py --merge-fallback` — 멱등 병합(중복 스킵), 원본은 `.merged-<ts>`로 보존.

## 4. 디스크 풀(G8)
- 증상: `_j` critical 기록실패 → 자동 HALT + stderr. 매매는 이미 동결 상태.
- 조치: 공간 확보(로그 `.1` 회전본·오래된 백업/미러 정리 가능 —
  `~/.toss-trader/{execute.log.1,alerts.jsonl.1,metrics.jsonl.1}`, `serenity/mirror/*`) →
  `--merge-fallback` → 1번 HALT 해제 절차.

## 5. 크론이 죽었을 때
- `--status`의 `last_run: N분 전` (정규장 2h↑면 ⚠️ 자동표시).
- 세션 크론은 Claude 세션 종료 시 함께 죽음 — 세션 재시작 후 관리 크론 재등록.

## 6. 키/토큰 사고
- 토큰 캐시는 기본 in-memory(디스크 저장은 TOSS_TOKEN_PERSIST=1 opt-in).
- 유출 의심 시: 토스 개발자센터에서 키 재발급 → 환경변수 교체 → `--selftest`.
- `--selftest`가 90일↑ 경과 시 로테이션 리마인더를 띄움(K2).

## 7. 전략 긴급 정지 / 재개
- 정지: `touch ~/.toss-mcp/HALT` (또는 env `TOSS_KILL=1` — 서버 주문 게이트).
- 재개: 1번 절차(원인 확인 없이 해제 금지).

## 8. 완전 재설치
```
git-less 배포라 파일 복사 기반: toss-mcp/ 통째 백업본 + ~/.toss-trader/(원장·설정) +
~/.toss-mcp/(가드 상태) 복원 → pip install -r requirements.txt → ./ci.sh → --selftest
```

원칙: **원장(index_journal.jsonl)이 진실.** 복구 중 어떤 단계에서도 원장을 직접 편집하지
말고, 보존(이동) 후 재구성하며, 마지막은 항상 `--verify-broker`로 브로커와 대사한다.
