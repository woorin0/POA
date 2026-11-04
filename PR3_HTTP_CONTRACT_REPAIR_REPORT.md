# PR3 HTTP 입력 계약 복원 / 2026-11-05

상태: 로컬 게이트 통과. 최종 B/C 패키징 및 Draft PR/CI 검증 대기.

## HTTP_MEDIA_CONTRACT / HTTP_REGRESSION

`main.py::_v1_request`에서 기존 FastAPI0.110.1의 Content-Type 해석을 복원했다.
Content-Type이 없거나 application/json, application/*+json일 때만 JSON을 해석하고,
그 외에는 원본 bytes를 기존 v1 ModelField에 전달한다. 빈 본문/null은 기존 missing
422로, JSON 오류는 기존 정제된 422로, 기타 본문 해석 오류는 기존400으로 처리한다.
주문·금융·Safe Retry·Outbox 실행 함수는 변경하지 않았다.

- 기존 HTTP65건(원래49건 + price/hedge/본문16건): 상태·응답 JSON·정규화된 주문값·호출 횟수 전부 동일.
- 미디어타입6종 × 정상/인증실패/null/빈본문/잘못된JSON/비정상UTF8: 36/36 동일.
- text/plain 및 application/octet-stream의 유효 주문JSON: 기준선과 후보 모두422, Fake 주문호출0.
- Whitelist 비허용 주소403 및 비인가 입력의 주문호출0 유지.
- 기대값은 실제 이전 FastAPI/Pydantic1 환경과 보존한 이전 소스로 생성했다.
  기존65건 기대값과 Golden은 변경하지 않았으며 새36건 Oracle만 추가했다.
- 증거: release_checks/http_contract_repair/http_comparison.json,
  http_contract_baseline.json, http_contract_product.json.

## FIVE_FAILURE_ROOT_CAUSES

이전 로그에는 node ID가 없었으므로 동일 수집 순서의164–168번과 전체 수집 상태에서의
선택 재현을 대조했다. 다음5건이 모두 같은 예외로 재현됐다.

1. test_completed_summary_wrong_symbol_must_not_publish[BINANCE]
2. test_completed_summary_wrong_symbol_must_not_publish[BYBIT]
3. test_completed_summary_wrong_symbol_must_not_publish[OKX]
4. test_completed_summary_wrong_symbol_must_not_publish[UPBIT]
5. test_trusted_confirmed_execution_after_create_ack_must_notify

분류: **5건 모두 테스트 환경 문제**. 이전 증거 보존 과정에서 만든
release_checks/final_integration/baseline/tests/conftest.py가 pytest에 자동 로딩됐다.
사본은 자신의 위치를 ROOT로 삼는 전역 감사훅을 추가해 정상 합성 증거 DB의 백업을
AUDIT_PRODUCTION_DB_DISABLED로 차단했다. 제품 HTTP/의존성 회귀로 분류할 근거는 없다.

파일 단독 실행5 PASS, 전체 수집 상태의 선택 실행5 FAIL로 원인을 입증했다.
사본을 .snapshot으로 이름만 변경해 바이트·원본 증거를 보존한 뒤 같은 전체 수집 상태에서
해당5건 + HTTP직접2건 **7 PASS**. 활성 tests/conftest.py, 브리지, 안전 조건은 변경하지 않았다.
이번 시작 시 만든 테스트 사본도 .snapshot으로 보존해 별도 모듈명 충돌을 방지했다.

원본 실패 JUnit: five_full_collection.xml. 수정 후: http_five_fixed.xml.
상세 분류: five_failure_classification.json. 기존 중단 로그도 그대로 보존한다.

## 완료한 안전 검증

- INDEPENDENT_TESTS: 63 PASS, 실제 Ed25519·체결금액·Admission·Outbox·설치 안전 경계.
- 영향 회귀: 102 PASS +4 subtests PASS, Safe Retry·Upbit Safe Retry·source-pin·금액 표시.
- MANIFEST_70: 실제 Bytes/SHA256 PASS.
- Manifest SHA256: 7dd5752b844ca46d7f038a382e688112a5554c3c16f681b6a506c5ad67da7280
- PROTECTED_FILES: 이번 제품 변경은 _v1_request뿐. 함수 외 main AST 동일;
  거래소/금융/Safe Retry/Outbox/DB/의존성/기존Golden/활성브리지 변경0.
- Source-pin은 기존5파일/동일 범위를 유지하며 변경 helper의 정확한 해시만 갱신했다.
- 최종 설치 freeze 재감사: 알려진 Python 취약점0, ignore/suppression 없음.
- Golden Stage1~3: 76/39/8 출력 원본과 일치.

FULL_WINDOWS_PYTEST: 1280 PASS / 0 FAIL / 0 ERROR / 기존 SKIP1 /4 subtests PASS, exit0, 1113.85초. SKIP node/사유는 이전 기준선과 동일함을 XML로 확인했다.
CODE_READY: YES (로컬 게이트). PUBLICATION_READY: YES (Draft PR 한정). RELEASE_CANDIDATE_READY: CI/패키징 대기.
OPERATING_READY: NO. 실제 운영 복원·Ubuntu VPS·거래소·Discord 연결은 미검증.

SERVER_DEPLOYED=NO / ORDERS_EXECUTED=NO / DISCORD_SENT=NO / MAIN_MERGED=NO.
