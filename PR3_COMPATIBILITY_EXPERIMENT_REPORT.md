# PR3 잔여 보안 호환성 실험

결과: PARTIAL / PUBLICATION_READY=NO. 제품 전환·Commit·Push·PR·운영 작업은 수행하지 않았다.

## 보존 및 실행 환경

- HEAD: `de969040dec42a369274adb1a59a3d44a2ba3aa6`, branch `fix/poa-release-safety-pr3`.
- 시작 시 보존한 기존 WIP 18파일의 raw SHA256 재검증: 불일치 0. `release_checks/compatibility_experiment/baseline_manifest.json`, `baseline.diff`, `baseline_files/` 보존.
- 이번 변경은 이 보고서와 `release_checks/compatibility_experiment/`의 격리 실험·증거뿐이다. requirements 및 기존 제품·테스트·Golden·Manifest 변경 0.
- Manifest 70항목 원문 SHA256: `d266d07331ed3af00f9355a3f3cf824935821f0db64b3b3ab83cfc5080d56591` (기존 값 유지).
- Windows Python 3.10.5, 별도 `.compat-sdk-venv`, `.compat-http-venv`; PocketBase는 합성 저장소/loopback의 자체 자식 프로세스만 사용. Ubuntu 실행 UNVERIFIED.
- 실제 거래소·Discord·Vultr·운영 DB·실제 credential 접근 0. 공개 패키지/공식 자료 다운로드와 로컬 합성 서버 통신만 수행.

## H11_RESOLVER / H11_FIXED_VERSION / H11_COMPATIBILITY

기존 제약은 `pocketbase 0.8.2 → httpx >=0.23,<0.24 → httpcore >=0.15,<0.17 → h11 <0.15`이다. 이 제약을 유지하며 h11 0.16.0을 설치할 수 없다.

조사한 배포본 중 최소 허용 SDK는 **pocketbase 0.12.2** (`httpx>=0.27.2`, Python>=3.9). 0.12.1까지는 httpx<0.25 제약이다. 선택 조합:

`pocketbase==0.12.2 / httpx==0.27.2 / httpcore==1.0.9 / h11==0.16.0`

선언된 제약을 준수하는 resolver와 pip check PASS. 기존 Pydantic 1.10.13/FastAPI 0.110.1 유지 가능. 실제 SDK로 관리자 인증·CRUD·페이지 조회·토큰 속성을 합성 서버 0.16.6/0.22.48에서 검증했다. 원래 `exchange/pocket.py` 호출 함수와 설치 bootstrap 함수를 AST로 추출해 그대로 실행했으며, 외부 API 성공을 Mock으로 대체하지 않았다.

제한: 기존 `get_full_list(query_params=None)`는 SDK 0.8.2와 0.12.2 모두 AttributeError다. 최초 실패 JSON을 보존했다. 현재 main의 실제 호출은 명시적 dict를 전달하며, 이 호출형의 실험은 PASS다. 기본 None 경로까지 정상이라고 주장하지 않는다. 최신 SDK 0.17.3은 legacy admins API가 정적으로 없어 최소 후보에서 제외했다(실행 검증 없음).

증거: `sdk_metadata.json`, `sdk_resolved.json`, `sdk_installed.json`, `pb_probe_0.8.2.json`, `pb_probe_0.12.2.json`, `pb_probe_0.12.2_explicit.json`, `latest_sdk_static_api.json`.

## STARLETTE_FIXED_VERSION

공식 권고의 7개 CVE를 모두 포함하는 최소 조사 수정선은 **Starlette 1.3.1**이다.

| CVE | 영향 조건 | 수정선 |
|---|---|---|
| CVE-2024-47874 | filename 없는 multipart field의 무제한 메모리 사용 | 0.40.0 |
| CVE-2025-54121 | 큰 multipart 파일 파싱 중 이벤트 루프 차단 | 0.47.2 |
| CVE-2026-48710 | Host 검증 누락으로 URL path 기반 검사 우회 | 1.0.1 |
| CVE-2026-48817 | HTTPEndpoint의 임의 method getattr dispatch | 1.1.0 |
| CVE-2026-48818 | Windows StaticFiles UNC 경로 SSRF/NTLM 노출 | 1.1.0 |
| CVE-2026-54282 | 경로/authority 결합으로 URL hostname 오염 | 1.3.0 |
| CVE-2026-54283 | urlencoded request.form 제한 무시로 DoS | 1.3.1 |

권고 원문/alias/수정 범위는 `release_checks/security_patch/remaining_advisories.json` 및 `starlette_multipart_advisory.json`에 보존했다. 각 기능을 앱이 실제 사용하는지에 따른 노출 여부와 패키지 취약 상태는 구분한다. 현재 JSON 주문 API만 시험했으므로 form/static 노출이 없다고 전체 면제하지 않는다.

공식 근거: [Starlette 보안 권고](https://github.com/encode/starlette/security/advisories), [FastAPI 릴리스](https://fastapi.tiangolo.com/release-notes/), [PocketBase SDK 배포](https://pypi.org/project/pocketbase/0.12.2/), [h11 권고](https://github.com/python-hyper/h11/security/advisories/GHSA-vqfr-h8mv-ghfj).

## FASTAPI_PYDANTIC_MIGRATION_SCOPE / HTTP_ORDER_CONTRACT_EQUALITY

FastAPI 0.125.0의 Pydantic v1 지원선은 Starlette<0.51이므로 1.3.1과 함께 쓸 수 없다. 실험 조합 `FastAPI 0.133.0 / Starlette 1.3.1 / Pydantic 2.9.2`는 resolver/pip check PASS지만 **현재 앱에 그대로 적용 불가**다.

- 기존 schemas의 BaseSettings import는 Pydantic v2에서 실패한다.
- 최신 FastAPI에 v1 모델을 직접 route DTO로 등록하면 PydanticV1NotSupportedError다.
- 제품 파일을 수정하지 않고 메모리 내 schemas import만 `pydantic.v1`로 보존한 HTTP 경계를 실험했다. 실제 main.order 본문·whitelist·오류 handler를 사용하고 외부 주문 어댑터만 Fake로 차단했다.
- 49개 JSON/validation/auth/whitelist 반례에서 주문 실행 판단·정규화된 전달값·HTTP 상태 **49/49 동일**, 전체 응답 **48/49 동일**. null body는 양쪽 422/주문호출0이지만 오류 envelope가 다르다. `http_comparison.json`에 보존.
- 원래 공유 models를 native v2로 로드하면 숫자 order_id/net_pnl의 문자열 변환이 v1과 달라진다. `model_native_v1.json`, `model_native_v2.json`. 이 차이를 허용하도록 테스트를 약화하지 않았다.

권장 최소 후속 범위(미구현): v1 호환 import를 일관되게 보존하는 모듈, `exchange/model/schemas.py`, `exchange/verified_pnl/models.py`, `exchange/pexchange.py`, `exchange/stock/schemas.py`, `main.py`의 `/order,/price,/hedge` HTTP wrapper 약 **6파일/60–110줄 + 테스트**. import만 바꾸더라도 보호된 거래 경계가 포함되므로 별도 승인 필요. null validation envelope까지 동등성 검증해야 한다. 전체 매매 엔진을 v2로 재작성해야 한다는 근거는 없다.

FULL_MIGRATION_REQUIRED: 전체 엔진 재작성은 입증되지 않음. 공유 v1 모델 보존 및 HTTP 경계의 제한형 호환 전환은 필요. 현재 범위에서는 전환 중단.

## POCKETBASE_CANDIDATE / API_COMPATIBILITY / DB_MIGRATION

후보 **서버 0.22.48 + SDK 0.12.2**. 공식 Windows amd64 ZIP을 배포 checksums와 대조한 후 실행했다. 운영 자료 없이 bootstrap_db로 합성 admin/kimp 컬렉션을 생성했다.

- 원본 0.16.6의 인증/CRUD/페이지 조회 PASS.
- 중지한 합성 원본의 SQLite backup API 스냅샷을 별도 위치로 복원하고 0.22.48로 열기: 기존 레코드/ID/필드 보존 및 추가 CRUD PASS.
- SDK 0.8.2, 0.12.2의 명시적 query dict 호출 각각 PASS. 현대 HTTP 조합+SDK 0.12.2에서도 PASS.
- 최종 `pb_probe_0.12.2_combined_auth.json`: 이전/새 서버에서 잘못된 암호와 미인증 컬렉션 접근 거부 PASS.
- 원래 중지 스냅샷을 별도 디렉터리에 복원 후 구 서버 읽기 PASS. 업그레이드 후 새 레코드는 이 과거 백업에 없으므로 운영 시작 후 자동 DB 롤백은 금지해야 한다.
- 구 바이너리가 새 DB 복사본을 열어 읽은 것은 **안전한 downgrade/쓰기/롤백 입증이 아니다**.
- 합성 스키마 변화: users.lastLoginAlertSentAt 추가; logs.db의 `_requests`가 사라지고 `_logs` 생성. kimp 컬럼 불변. 기존 요청 로그 전체 보존은 입증하지 못했다. `pb_schema_delta.json` 참조.
- 최초 실험의 기본 migrationsDir 공유는 컬렉션 중복 오류를 유발했다. 실험별 dir/publicDir/cwd를 분리한 뒤 재검증했다. 실패 증거를 보존했으며 제품 결함으로 수정하지 않았다.

공식 수정선으로 기존 3건(GHSA-m93w-4fxv-r35v, GHSA-pq7p-mc74-g65w, GHSA-84vh-m24q-wjjx)은 0.22.48에서 해결된다. [공식 릴리스](https://github.com/pocketbase/pocketbase/releases/tag/v0.22.48), [보안 권고](https://github.com/pocketbase/pocketbase/security/advisories). 운영 DB·OAuth·첨부파일·복잡한 컬렉션·Linux 실행·실복원·롤백 검증은 UNVERIFIED. Go 바이너리 전체 의존성 보안 감사도 UNVERIFIED다.

## CVE_RESCAN / INDEPENDENT_TESTS

| 실행 | 실제 결과/증거 |
|---|---|
| SDK 후보 실제 Ed25519·금액·Admission·Outbox·설치 독립 검사 | 63 PASS, `sdk_independent.xml` |
| SDK 후보 Safe Retry/Upbit Safe Retry/의존성 계약 | 77 PASS, `sdk_contracts.xml` |
| 현대 HTTP 후보 동일 독립 검사 | 63 PASS, `http_independent.xml` (공통 테스트 재실행이며 고유 126개로 합산하지 않음) |
| 기존 v1 Golden Stage 1–3 | 76/39/8 출력 기존 Golden과 동일, exit0, `sdk_golden_results.json` |
| SDK 후보 전체 설치 freeze 재감사 | Starlette raw14건/중복제거 CVE7건 잔류; h11 미검출, `sdk_audit.json` |
| 현대 HTTP+SDK 후보 전체 설치 freeze 재감사 | 알려진 Python 취약점 0, exit0, `combined_audit.json` |

감사는 실제 설치된 전체 freeze를 입력한 pip-audit이며 ignore/suppression 없음. 이는 Go 서버 포함 전체 시스템의 취약점0 보장이 아니다. 브리지/서명 검증 Mock 확대 없음. 후보 미확정 상태라 전체 pytest는 반복하지 않았다. 이전 Windows 전체 **1277 PASS/1 FAIL/1 기존 SKIP** 증거는 유지한다. 해당 source-pin 집중 재검사69 PASS를 전체0FAIL로 재해석하지 않는다.

## 권장 최소 경로와 잔여 차단

1. h11: 검증한 SDK0.12.2/httpx0.27.2/core1.0.9/h11.16.0 조합을 후속 패치 후보로 채택 가능. 아직 root 의존성에 적용하지 않았다.
2. Starlette: 위 6파일 범위의 공유 v1 보존/HTTP wrapper 설계 별도 승인 후 정확한 오류/직렬화 동등성 보완. Pydantic2 전역 강제 전환 금지.
3. PocketBase: 0.22.48의 합성 마이그레이션 가능성은 입증했으나 실서버 외부 백업·동시점 복원·로그 보존·코드/DB 롤백 검증 전 운영 업그레이드 차단.

PUBLICATION_READY=NO / CODE_GATE=BLOCK / OPERATING_READY=NO.
SERVER_DEPLOYED=NO / ORDERS_EXECUTED=NO / DISCORD_SENT=NO / GITHUB_CHANGED=NO.
