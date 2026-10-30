# POA source-pin 정밀화 및 코드 게이트

BASE_SHA: 3a9523341ac75a550b31b4f9c090258814d91f76
범위: 두 source-pin 검사와 기존 알림·금융·송신 안전 회귀. 제품 기능 변경 없음.

## 원인과 수정 범위

수정 전 원문 67파일 manifest를 검증한 LF checkout에서 대상 두 검사가 모두 FAIL이었다 (`before.xml`, exit1).
Windows 기본 checkout의 CRLF 변환으로 발생한 별도 초기 하네스 실패는 다른 디렉터리에 보존했다. 원문 정합 실패와 실제 source-pin 충돌을 구분했다. 원래 작업공간과 기존 로그는 덮어쓰지 않았다.

- test_order_and_crypto_contracts_unchanged: 과거 c99be386의 OrderRequest 전체 AST를 고정해, 이미 승인된 signal_id/signal_time binding 검증 추가를 거부했다.
- test_main_order_and_financial_guards_unchanged: 과거6a84d84b의 delivery_store.py 전체 bytes를 고정해, unsigned 알림 snapshot3의 엄격한 admission 지원을 거부했다.

허용 범위는 다음 **5개 source span의 정확한 SHA256**뿐이다. 승인 기준 커밋은 3a952334 전체 SHA로 고정한다.

1. OrderRequest.validate_tv_binding
2. Settings.POA_NOTIFICATION_SERVER_ALIAS (정확한 선언과 기본값 None)
3. NotificationOutboxStore.submit_observed
4. NotificationOutboxStore._estimated_authority
5. NotificationOutboxStore._buy_notification_authority

이름만 지정해 함수를 검사에서 제외하지 않는다. 각 source span은 decorator·본문을 포함한 승인 원문 hash와 정확히 같아야 한다. 승인 span을 과거 원문으로 치환했을 때 파일 전체가 기존 역사적 blob과 byte 단위로 같아야 한다. 임의의 다른 줄·함수·class·import 변경과 중복 선언도 거부한다.

schema는 기존 일부 class AST 비교보다 강한 **전체 파일 잔여 bytes 보호**로 바꿨다. 기존에는 별칭 필드를 통째로 무시했지만 이제 그 필드의 타입·기본값도 고정한다.
기존 주문 진입점 order/schedule_order_log/log/get_error AST와 _wire/_attempt/begin_post/ack/claim_next/_notice_gate AST 검사도 그대로 유지한다.
주문 실행·수량·Safe Retry·VERIFIED·원장·서명·HISTORY·실제 sender의 기존 원문 pin은 유지한다.
새 부정 대조군은 두 번째 검사의 reconciliation 단독 pin 부재를 발견했다. 첫 번째 검사에는 이미 있던 보호이며, 두 번째에도 추가해 강화했다. 광범위한 예외로 통과시키지 않았다.

## 검증 및 증거

최종 수치와 실행 exit는 로컬 `audit_artifacts/source_pin_gate/` JUnit 및 summary.json에 기록한다.
기존 173개 기능 검사를 모두 재실행하고, 정밀화한 source-pin 2개와 부정 대조군 30개를 추가한다.
부정 대조군은 제품 파일을 수정하지 않고 메모리상의 소스만 바꾼다. 주문 수량·비밀번호·정규화·Safe Retry·금융 파일·원장·서명·송신·ACK·lease·매도 admission·승인된 매수 admission·signal binding 변경이 실제 실패하는지 확인한다.

다음 실제 기능·적대적 회귀를 유지한다.
- 4개 거래소 매수·매도, 독립 Decimal Oracle, 분할매수·부분매도·203전략 격리.
- 매수에 PnL/ROI/금융 receipt 없음, 위조 producer·잘못된 계정/전략/종목/side·변조 snapshot·신호 충돌 거부.
- 실제 API 없는 producer→Intake→Worker→Outbox→frozen bytes→Mock HTTP 응답→ACK.
- VERIFIED 우선, 원가151/Net8, 거짓Net58 차단, 기존 v4 canonical/signature 및 KRW v5 양성.
- HISTORY successor 철회와 POSTING ACK race, SENT 불변, UNKNOWN/HELD 자동 POST0.
- 10 producer/2 Worker 경합, 재시작·ACK gap·오래된 v4 복원 차단, 기존 v2 Intake digest/wire 호환.
- Safe Retry 대표7건 별도 프로세스.

기존 과거 UNKNOWN/HELD raw head=-2 요구 2 FAIL은 이미 승인된 별도 기준선 예외다. 이번 source-pin 2건과 다른 검사이며, 이번 작업에서 삭제·변경·재분류하거나 PASS로 계산하지 않는다.

## 제품·릴리스

제품 67파일은 BASE_SHA와 원문이 동일하다. manifest 및 Ubuntu User Data도 동일한 해시로 이미 동기화되어 있으므로 불필요하게 갱신하지 않는다.
RELEASE_MANIFEST_SHA256: 3ee5c14bfdaddcf0930c1e0951fad83418c96de0d822d337ecdbe4a24499068b
테스트·test-only pin helper·이 보고서만 반영하며, DB·로그·JUnit·개인키·credential은 업로드하지 않는다.
실제 API·거래·Discord 송신·Vultr 배포·운영 DB 접근은 수행하지 않는다.

## 판정 범위

CODE_GATE는 현재 지원하는 4개 현물 TradingView 알림 파이프라인과 명시한 보호 회귀 범위의 코드 게이트다. 실계정 금융 증거 공급이나 전체 금융 시스템 운영 승인으로 확대하지 않는다.
실제 Vultr→Discord 수신·운영 credential/provider·장기 운영 성능은 UNVERIFIED이고 OPERATING_READY=NO를 유지한다.
서버 간 전역 중복 방지와 불확실 delivery의 자동 정정/release는 기존 미지원 범위다. 새 자동 승인 경로는 추가하지 않는다.
최종 판정·검사 집계는 아래 완료 기록에 추가한다.


## 완료 기록

- 대상 두 검사: 수정 전 2 FAIL / exit1 → 수정 후 2 PASS / exit0.
- 기존 기능173개: 전부 재실행 PASS (동일 제품 bytes, 대규모 전체 감사는 반복하지 않음).
- 전체 결합 실행: 197 PASS / 1 FAIL / exit1. 유일한 FAIL은 새 부정 대조군이 발견한 두 번째 검사 reconciliation pin 누락이었다. 제품 결함이 아니며, 첫 번째 기존 검사에는 이미 보호가 있었다.
- 해당 검사 보호를 강화한 뒤 두 원본+30개 부정 대조군만 재실행: 32 PASS / 0 FAIL / exit0. 성공한 기능173건은 불필요하게 반복하지 않았다.
- Safe Retry 별도 프로세스: 7 PASS / 0 FAIL / exit0 (기능173개에 포함, 중복 합산 안 함).
- node별 최종 결과 합집합: **205 PASS / 0 FAIL / 0 ERROR / 0 SKIP** = 기존 기능173 + source-pin2 + 부정 대조군30.
- CODE_GATE=PASS — 현재 지원 현물 TradingView 알림 및 명시한 코드 안전 계약 범위.
- OPERATING_READY=NO — 실제 서버/거래소/Discord 검증·배포는 수행하지 않음.
- 신규 제품 변경0, 신규 금융·송신 기능 회귀0. 기존 UNKNOWN/HELD 기준선 예외 및 미지원 기능은 그대로다.
- 제품 manifest와 User Data 핀 검증 PASS. 제품 bytes가 같으므로 SHA256은 변경하지 않는다.

원본 JUnit/log와 중간 실패는 보존한다. 최종 수치는 모든 실행을 무작정 합산한 수가 아니라, 동일 node의 필요한 재검증을 반영한 고유 검사 결과다.
