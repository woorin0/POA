# Discord 오류 신고 복구 및 색상 정책

BASE_SHA: b7798cbd2d1f4400dc61ec1768d3374b20f08fd4
RESULT: PASS (알림 Mock/소스 검증 범위)
OPERATING_READY: NO

## 확인된 결함 / 실서버 미검증

- 금융 알림 Worker/서명 거절 경로만으로는 일반 API·관측·Worker 장애를 운영 오류로 전달할 수 없었습니다. 금융 증거 승인 조건을 유지하면서 별도 운영 장애 유형을 연결했습니다.
- ID-only 매수 ACK 후 조회가 실패하면 접수 알림도 누락되던 경로를 복구했습니다. 실제 체결로 승격하지 않으며 REVIEW 원본을 보존합니다.
- 매수/매도 색상과 체결·금융 검증 상태가 혼동됐습니다. 새로 admission한 이벤트에만 표시 정책을 영속화합니다.
- 실제 주문 adapter는 ccxt 예외를 OrderError로 감쌉니다. 최대 4개 cause/context의 타입만 검사해 고정 코드로 분류하며 예외 원문은 사용하지 않습니다.
- BOT03의 실제 2026-10-31 장애 원인은 UNVERIFIED입니다. 실제 서버/계정/주문 JSON/Discord 채널에 접근하지 않았습니다. Mock 401 재현을 실서버 인증 오류의 증명으로 취급하지 않습니다.

## ERROR_ALERT_IMPLEMENTATION / 전달 경로

주문·조회·Intake·admission·복원·Worker·Discord 오류 → 고정 코드/안전 metadata → 독립 ops SQLite commit → 별도 reporter → 기존 DiscordSender.send_frozen → HTTP Mock/ACK.

`operational_alerts.py`는 주문/금융 payload를 받지 않습니다. 기존 주문 Outbox와 별개인 운영 장애/복구 문구만 생성하며, 주문 실행·재실행·금융 서명·REVIEW 승인·UNKNOWN/HELD release 권한이 없습니다. 기존 DISCORD_WEBHOOK_URL을 사용하므로 새 URL/키 입력은 필수가 아닙니다.

- Journal: `<VERIFIED_NOTIFICATIONS_DB_PATH>.ops.sqlite3`, WAL/FULL, 영속 POST intent, token/digest CAS.
- 같은 서버·안전 코드·주문 지문·단계의 열린 장애 100회 → 1개 사건/발송. 별도 복구 후 재발은 새 episode입니다.
- 전체 reporter 최대 6회/분. 명시적 429만 최대 3회. 5xx/Timeout/전송 중 중단은 UNKNOWN, 명시적 거부는 HELD로 보존하며 자동 재송신하지 않습니다.
- Worker tick의 5초 내 회복된 짧은 오류는 송신하지 않습니다. 정상 미체결/확인 대기는 오류가 아닙니다.
- 실제 관측 성공, 해당 admission 성공, 복원 preflight 완료, Worker 정상 tick에서만 해당 단계의 복구를 판정합니다. admission 복구로 DISCORD_DELIVERY_UNKNOWN을 지우지 않습니다.
- 외부 전달/주문 상태가 아직 불명확한 장애는 시간 경과만으로 복구하지 않습니다. 복구 메시지도 주문 체결/메시지 전달 성공을 뜻하지 않습니다.
- Worker/오류 reporter 시작 실패는 비밀정보 없는 코드로 진단합니다. reporter 실패가 주문을 재실행하거나 정상 주문 경로에 예외를 전파하지 않습니다.
- 시스템 전체 중단, 저장장치 고장, Journal commit 실패, Discord 장애에서 실제 수신은 보장할 수 없습니다. 고정 로컬 진단 코드와 외부 운영 모니터링이 필요합니다. 여러 서버 간 전역 중복 방지는 제공하지 않습니다.

안전 코드 예: ORDER_AUTH_ERROR/PERMISSION_ERROR/PARAMETERS_ERROR/TIMEOUT/NETWORK_ERROR, UPBIT_READ_AUTH_REJECTED/PERMISSION_REJECTED/IP_REJECTED/RATE_LIMITED/TIMEOUT/SERVER_ERROR/ORDER_UUID/SCOPE_MISMATCH/QUANTITY_CONFLICT/FILL_INCOMPLETE, UPBIT_OBSERVATION_DB_FAILED/ADMISSION_FAILED/REVIEW, BUY_ADMISSION_REJECTED, SIGNAL_SELL_ADMISSION_REJECTED, NOTIFICATION_INTAKE_REVIEW, INTAKE_SAVE_FAILED, WORKER_START_FAILED/NOT_STARTED/TICK_*, RESTORE_PREFLIGHT_BLOCKED, DISCORD_DELIVERY_FAILED/UNKNOWN. 포괄 코드만으로 세부 원인을 추정하지 않습니다.

## 신규 색상

| 새 이벤트의 근거 | 색상 | 금융 의미 |
|---|---|---|
| 등록된 경로의 실제 체결 확인 | #2563EB BLUE | 매수·매도 동일. 금융 미확정은 별도로 표시 |
| 실제 오류 이벤트 | #DC2626 RED | 주문 실패 확정을 의미하지 않음 |
| 체결 미확인·유효한 예상 손익 | #8B5CF6 PURPLE | TradingView/기록 기반 추정 |
| 정상 접수·확인 대기 | #64748B GRAY | 체결 완료로 표시하지 않음 |

색상은 payload 플래그만으로 승인되지 않습니다. 정상 admission 이후 등록된 근거로 새 marker를 설정하며, 위조 marker는 제거합니다. marker 없는 역사적 payload는 기존 renderer 결과를 유지합니다. 과거 SENT wire/상태/message ID/ACK를 재작성하거나 다시 발송하지 않습니다.

## BOT03 Mock SQLite 증거

`audit_artifacts/discord_alert_policy/bot03_state_summary.json` 및 `bot03_sqlite/` (로컬에만 보존):

- Intake SUBMITTED 1
- job OBSERVED_REVIEW 1
- 기존 주문 Outbox: 회색 접수 SENT 1
- 운영 오류 Journal: UPBIT_READ_AUTH_REJECTED, 빨간 오류 SENT 1
- 조회 확정 전 BLUE 0. 별도 검증에서는 이후 실제 체결 관측으로 같은 root의 새 revision을 만들며, 원본 SENT는 보존합니다.
- UNKNOWN/HELD/POSTING 및 후속 ACK 경합에서 무승인 재송신 0.

## 검증 / 과거 실패 보존

최종 고유 집계: 414 PASS / 0 FAIL / 0 ERROR / 0 SKIP. 중복 실행은 합산하지 않았습니다.

- 동결 후 기존 영향 회귀: 208 PASS / exit0 (`frozen_regression.xml`).
- 신규 정책/source-pin: 96 PASS / exit0.
- 승인된 GET: 49 PASS / exit0.
- Safe Retry: 42 PASS, Upbit Safe Retry: 21 PASS / 각각 exit0.

BLUE_FILL_TEST / RED_ERROR_TEST / PURPLE_ESTIMATE_TEST / GRAY_PENDING_TEST / UPBIT_LINK_REPRODUCTION / DUPLICATE_SUPPRESSION / FROZEN_WIRE_REGRESSION: PASS. UNKNOWN/HELD 무승인 POST 0, Canary 노출 0, 금융·송신 신규 회귀 0. 모든 주문·거래소·Discord transport는 Fake/Mock이며 격리 SQLite만 사용했습니다. 네트워크·운영 DB guard 및 실제 import 경로 검사를 사용했습니다.

- 운영 정책·색상·중복·보호 source-pin: release_policy.xml 96 PASS / exit0.
- Safe Retry 독립 실행: 42 PASS, Upbit Safe Retry 21 PASS / 각각 exit0.
- 두 Safe Retry 하네스를 한 프로세스에서 수집하면 모듈 재초기화 때문에 class/설정 참조가 어긋나 3 FAIL이 발생했습니다. 원본 결과를 보존하고 독립 프로세스 결과와 구분합니다.
- 승인 전 GET 원본: 45 PASS / 4 FAIL / exit1. 수정 전 main에서도 동일한 4 FAIL이었습니다.
- 사용자 한정 예외 승인 후 GET: 49 PASS / exit0. SELL payload를 명시 선택하고 BUY+SELL 2개 ACK를 검증합니다. 플랫폼 함수 전체를 Mock하지 않고 외부 vault/binding 발견 경계만 등록된 테스트 capability로 대체했습니다. 별도 프로세스 재시작 후 추가 POST 0.
- 네 GET 함수 외 AST 선언 불변 및 원본/변경 SHA256: approved_get_exception.json. 승인 전 원본과 JUnit/log는 삭제하지 않았습니다.
- Worker 테스트는 HTTP 관측과 tick 완료 사이의 경합을 발견했습니다. 동일 12초 한도 안에서 두 관측과 tick 완료를 함께 기다리도록 하네스를 보완했으며, 원래 Assertion/재시작 중복 검사는 그대로 유지합니다.
- source-pin 보완을 진행 중 회귀에 반영한 실행에서는 초기 캐시와 파일이 달라 1 FAIL이 났습니다. 이 로그를 보존하고 최종 bytes를 동결한 새 프로세스의 결과를 별도 기록합니다.
- 과거 색상 기대 1건은 새 BLUE 규칙으로 정밀 변경하고, 원가 없음·금융 receipt 없음·금융 미확정 표시 검사를 강화했습니다.
- 기존 역사적 UNKNOWN/HELD 원본 2 FAIL의 승인된 계약 차이는 유지합니다. 이 두 건을 이번 GET 예외와 혼합하거나 PASS로 바꾸지 않았으며 이번 실행에서 재실행하지 않았습니다.

## 보호 / 릴리스

주문 실행·수량·Safe Retry·금융 VERIFIED·서명·HISTORY·원장·reconciliation·sender 12개 관련 파일은 기준 main과 byte 단위 동일합니다. main.order는 정확한 운영 오류 신고 2문장만 허용하고 나머지 AST를 원본과 비교합니다. Outbox _wire/_attempt/begin_post/ack/claim_next, 금융 및 API 보호 경계는 기존 source 검증과 신규 부정 검사로 유지합니다.

제품 raw SHA256/실행 경로/최종 freeze: audit_artifacts/discord_alert_policy/의 manifest 파일들. 합성 SQLite·로그·XML·원본 응답·임시 pin·개인키·캐시는 GitHub 업로드에서 제외합니다. 로그에는 안전 코드만 사용하며 API Key/Secret/계정 참조/주문 원문/JWT/Webhook URL을 출력하지 않습니다.

RELEASE_MANIFEST_SHA256: 1414d171ef345ab353b81711184f4c71cb53c21aad634b750edb615819f69efc
MAIN_SHA: 본 보고서를 포함하는 Git 커밋 및 최종 응답에서 확인. 자기 참조 SHA는 문서에 삽입하지 않음.

## VULTR_UPDATE_STEPS / 운영 미검증

[NOTIFICATION_RECOVERY_UPDATE.md](NOTIFICATION_RECOVERY_UPDATE.md)의 정지→.env/DB/Sidecar/WAL 동시점 백업→fast-forward 업데이트→manifest 검증→운영자 시작 절차를 사용합니다. 새 ops Sidecar도 보존해야 합니다. 재설치·DB 초기화·UNKNOWN/HELD 재전송은 금지합니다. BOT03 한 대부터 운영자가 순차 적용하며 이번 작업에서 서버 접속·배포·서비스 재시작은 하지 않았습니다.

실제 Vultr→Discord 채널 수신, 실계정 조회, Ubuntu 장기 실행/자원 사용, 디스크 장애 시 외부 모니터링은 UNVERIFIED입니다. Mock PASS를 실서버 성공으로 표시하지 않습니다. OPERATING_READY=NO.
