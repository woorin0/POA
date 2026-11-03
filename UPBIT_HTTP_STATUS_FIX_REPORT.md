# Upbit HTTP 401/403 상태 보존 수정

BASE_SHA: 15f8417ddcd9c9e68950b75e19fc58d0d9bf3117
CONFIRMED_DEFECT: HTTPSOrderGETTransport가 200/401/403 본문을 읽은 뒤 상태를 항상 200으로 반환했습니다. 인증 오류 envelope가 ORDER_RESPONSE_INVALID(SCHEMA)로 처리되는 코드 결함을 Fake HTTPS로 재현했습니다.
LIVE_CAUSE_UNVERIFIED: 실제 운영 응답의 HTTP 상태/원문은 확인하지 않았습니다. 제보된 SCHEMA_INVALID를 인증/IP/권한 오류로 단정하지 않습니다.

## 최소 변경

제품 변경은 exchange/verified_pnl/upbit_notification_get.py의 반환값 한 줄입니다.

`OrderGETResponse(200, ...)` → `OrderGETResponse(response.status, ...)`.

기존 JWT HS512/query_hash/nonce, 고정 HTTPS host/path, TLS 인증, 시간·응답 크기·encoding 제한, 인증 실패 latch 및 재시도 정책은 변경하지 않았습니다. 기존 error.name의 두 허용 이름(no_authorization_ip/out_of_scope)만 상태 기본 분류를 세분화하고, message/원문은 사용하지 않습니다. 주문 실행·Safe Retry·Intake·Worker·금융 계산·Outbox CAS/ACK 및 다른 제품 69파일은 기준 raw hash와 동일합니다.

[Upbit 공식 REST 오류 안내](https://docs.upbit.com/kr/reference/rest-api-guide)는 out_of_scope가 API 영역에 따라 401 또는 403으로 올 수 있어 HTTP 상태와 error.name을 함께 확인하도록 안내합니다. 이번 수정은 기존 허용 목록의 의미를 바꾸지 않습니다.

## HTTP_401_403_REGRESSION

실제 HTTPSOrderGETTransport → UpbitNotificationGET → buy_ack.error_code를 실행하고 HTTPSConnection만 Fake로 대체했습니다.

- 401 인증 실패 → UPBIT_READ_AUTH_REJECTED
- 401/403 no_authorization_ip → UPBIT_READ_IP_REJECTED
- 401/403 out_of_scope 및 일반 403 → UPBIT_READ_PERMISSION_REJECTED
- message 속 문자열/알 수 없는 name/비정상 error 구조는 추정 분류하지 않음
- 인증 실패의 네트워크 시도 1회, 이후 AUTHENTICATION_DISABLED로 추가 시도 0
- 302/429/500/Timeout/TLS, size/encoding 제한 및 재시도 시 새 nonce 회귀 유지

수정 전 선택 검사: 4 PASS / 8 FAIL / exit1. 수정 후 신규 직접 전체: 45 PASS / 0 FAIL / exit0.

## VALID_200_SCHEMA_REGRESSION

HTTP 200 정상 native XRP/KRW 주문은 그대로 반환합니다. 잘못된 JSON/UTF-8/중복 key/NaN/root 형태/error envelope는 계속 SCHEMA_INVALID입니다. UUID·market·side 불일치는 UUID/SCOPE 오류로 남습니다. 200 error JSON을 임의 인증 오류로 재해석하거나 성공으로 승격하지 않습니다.

등록된 Mock Host와 실제 native 정규화 경로에서도 정상 주문을 관측하고, 지원하지 않는 ord_type·수수료 NaN·잘못된 시간은 OBSERVED_REVIEW + UPBIT_READ_SCHEMA_INVALID, 체결 관측 0으로 남는 것을 확인했습니다. 접수 알림이 있더라도 체결 완료·금융 VERIFIED로 승격하지 않습니다.

## TOTAL_PASS_FAIL / 원본 보존

최종 고유 node: 240 PASS / 0 FAIL / 0 ERROR / 0 SKIP. 관련 회귀 177 PASS, 독립 Safe Retry 42 PASS, Upbit Safe Retry 21 PASS / 모든 exit0. 중복 실행은 합산하지 않았습니다.

- 신규 직접 45 PASS / exit0 (변경 전 원본 실패 증거 보존).
- 관련 결합 실행 final.xml: 167 PASS / 4 FAIL / exit1.
- 정확한 기준 SHA의 독립 전체 checkout에서도 같은 원본 4 FAIL / exit1.
- 추가 6개 현행 BUY+SELL 두 사건 검사는 재시작·SENT/UNKNOWN/HELD/POSTING·ACK token CAS·source reference 교차 결합 방지를 실제 구현으로 검증했습니다.

원본 네 검사:
- test_repeat_query_and_restart_keep_originals
- test_states_and_wire_cas_no_repost[SENT]
- test_states_and_wire_cas_no_repost[POSTING]
- test_same_actual_order_cannot_repost_under_new_source_reference

세 건은 BUY+SELL 2건을 만든 뒤 과거 SELL 단독 1건을 기대하는 최초 Assertion에서 실패합니다. POSTING 건은 첫 BUY만 claim하고 다른 SELL을 PENDING으로 남긴 상태에서 전체 POST0을 기대합니다. 승인 전 실행에서는 원본을 변경하지 않았고 해당 FAIL 로그를 보존했습니다. 이후 명시 승인된 함수 안에서만 BUY+SELL 두 delivery의 정확한 selector로 같은 안전 요구를 유지·강화했습니다. 원래 실패 증거를 삭제하거나 새 PASS로 덮어쓰지 않았습니다.

기존 source-pin/금융 canonical/정상151·Net8·관측 REVIEW/서명 VERIFIED 우선·중복·Canary 검사를 포함했습니다. 실계정 credential/거래소 API/주문/실제 Discord/운영 DB 접근 0입니다. 사용자가 지정한 세 함수(네 실패 케이스)만 정밀 수정했습니다. 기존 4 FAIL → 4 PASS, 같은 함수의 UNKNOWN/HELD도 포함해 6 PASS입니다. 주문 ID 및 root/revision으로 두 사건을 선택하고 관측/digest/SENT bytes/message ID 불변, 정확한 SELL POSTING과 ACK token CAS를 확인합니다. 함수 밖 원문 bytes가 동일함을 검증했습니다. 삭제·skip·xfail·기준선 예외 릴리스는 없습니다.

## 릴리스

MANIFEST_SHA256: ca3eb2524538e1b98e1184a7e2ccb0e69a3a555bdfe3431cdc82c7be974fb65b
CODE_GATE: PASS (이번 HTTP 상태 보존 패치 및 관련 Mock/소스 검증 범위)
OPERATING_READY: NO

제품 커밋을 먼저 만들고 User Data는 그 정확한 40자리 제품 커밋과 manifest를 후속 커밋에서 고정하는 방식으로 준비합니다. 이는 Git 커밋 자신의 SHA를 자기 내용에 넣을 수 없는 자기 참조를 피합니다. User Data 후속 커밋은 제품 bytes를 바꾸지 않습니다.

기존 REVIEW 주문을 자동 재조회·재발송하지 않습니다. 수정은 이후 허용된 조회에서 실제 상태를 올바르게 분류할 뿐입니다. Vultr 배포·재설치·서비스 재시작 및 운영 DB 변경은 수행하지 않습니다.

로컬 증거: audit_artifacts/upbit_http_status/의 before.xml, final.xml, baseline_contracts.xml, final_direct.xml, 로그, runtime_manifest.json, results_summary.json. 합성 DB/로그/XML/키/임시 결과는 업로드하지 않습니다.

PRODUCT_COMMIT_PIN: e53cc55eb46cc56c07b6ca0b0b7c7993b5507573
User Data는 최종 제품 커밋을 후속 메타데이터 커밋에서 고정합니다. 운영 환경 성공을 의미하지 않습니다.

USER_DATA_PIN: repo_ref=e53cc55eb46cc56c07b6ca0b0b7c7993b5507573, release_manifest_sha256=ca3eb2524538e1b98e1184a7e2ccb0e69a3a555bdfe3431cdc82c7be974fb65b. 최종 main의 후속 커밋은 User Data와 문서만 변경하며 제품 bytes는 동일합니다.
