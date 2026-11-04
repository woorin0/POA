# Dependency / PocketBase security review — PR3 local patch

## 최종 통합 후보 (이 절이 아래 과거 판정을 대체함)

2026-11-04: 검증한 FastAPI 0.133.0 / Starlette 1.3.1 / Pydantic 2.9.2를 적용하되,
기존 4개 모델 파일은 pydantic.v1을 사용한다. main의 HTTP wrapper만 v1 ModelField로
기존 검증을 호출한다. 원래 주문·Safe Retry·금융·Outbox 코드는 변경하지 않는다.
PocketBase SDK 0.12.2 / httpx 0.27.2 / httpcore 1.0.9 / h11 0.16.0을 사용한다.
기존 dotenv extra는 v1 BaseSettings에 필요한 python-dotenv 1.2.4 직접 의존성으로 보존했다.
CCXT 4.4.20 등 그 외 직접 의존성은 유지한다.

새 Python3.10 venv의 pip 22.0.4에서 설치 도구 취약점이 실제 재발견됐다.
기존 격리 실험에서 쓴 pip 26.3을 설치기/CI에 고정하고, 전체 설치 freeze 재감사 결과
알려진 Python 취약점 0 (ignore/suppression 없음). 이전 실패 감사도 보존한다.
증거: release_checks/final_integration/audit.json, audit_final.json, pip_check.txt,
http_comparison.json, independent.xml. 최종 전체 테스트/CI 판정은 최종 보고서로 확인한다.

Ubuntu 신규 staging 설치에만 PocketBase 0.22.48 Linux amd64 ZIP SHA256
`1b461b6e558884b0c6ccd558c4c989c48944dfdb9c719c467306008b5baa1b1e`를 고정했다.
기존 Debian 설치 경로는 변경하지 않았다. 0.16.6 운영 DB를 자동 기동/이관하지 않는다.
SDK/서버 인증·CRUD·합성 DB 이동의 성공 근거는 PR3_COMPATIBILITY_EXPERIMENT_REPORT.md에 있다.
Go 바이너리 전체 취약점 스캔, 운영 DB 복원·로그 이력 보존·안전한 downgrade는 UNVERIFIED.
서버 3개 권고의 공식 수정선 도달과 운영 복원 성공을 구분한다.

아래 내용은 이전 단계의 감사 이력이다. 과거 취약점 잔류 판정을 삭제하거나
최종 전체 회귀의 PASS로 재해석하지 않는다. OPERATING_READY=NO 유지.

기존 WIP·원본 감사는 security_patch/*.before.snapshot 및 before.diff에 보존했다.

## 중복 제거와 해결 비교

- 최초 직접 의존성 감사: 24개 보고 레코드 → CVE/GHSA 별칭 기준 **18개 그룹**.
- 동일 조건의 전체 해결 의존성 감사: 수정 전 40개 레코드 / 26개 그룹 → 수정 후 16개 레코드 / 8개 그룹.
- 최초 4개 직접 의존성의 18개 그룹은 최종 감사에서 보고되지 않는다. ignore/suppression 없음.
- 전이 h11/Starlette의 8개 그룹은 기존부터 있었으며 그대로 릴리스 차단이다. PocketBase 서버 권고는 pip 감사 밖에서 별도로 추적한다.

## 최소 패치

| 직접 패키지 | 이전 → 최종 | 이유 |
|---|---|---|
| PyJWT | 2.7.0 → 2.15.1 | 보안 수정 2.15.0 + 기존 padding 토큰 호환 수정 2.15.1 |
| FastAPI | 0.99.0 → 0.110.1 | 최소 보안판 0.109.1은 Starlette client=None으로 Safe Retry 통합 3건 실패. 공식 client scope 복원 0.37.2를 허용하는 최소판 |
| Pydantic | 1.10.10 → 1.10.13 | CVE 수정; v1 유지 |
| orjson | 3.9.1 → 3.11.6 | loads/dumps 재귀 제한 수정 |

- 의존성 resolver의 차이는 위 4개와 전이 Starlette 0.27.0→0.37.2뿐이다. CCXT 4.4.20, PocketBase Python 0.8.2, httpx 0.23.3 등 나머지 직접 핀은 그대로다.
- 새 Python 3.10 가상환경 설치와 pip check: 정상. 기존 환경은 변경하지 않았다.
- 초기 후보의 3 FAIL 및 수정 후 영향 검사 222 PASS/exit0 증거를 보존했다. 기존 테스트·브리지·골든·주문/미들웨어는 바꾸지 않았다.

## 원래 18개 그룹

심각도 숫자는 OSV 공식 CVSS 3.1 vector의 base score 계산값이며, 실제 POA 악용 가능성을 뜻하지 않는다. 모든 원천 vector/별칭은 findings_deduplicated.json에 보존했다.

| 패키지/의존 형태 | CVE/GHSA | 심각도 | 실제 사용 경로 및 도달 제한 | 최초 수정판 |
|---|---|---|---|---|
| pyjwt / 직접 | [CVE-2026-32597](https://osv.dev/vulnerability/CVE-2026-32597) / GHSA-752w-5fwx-jx9f | 7.5 HIGH | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.12.0 |
| pyjwt / 직접 | [CVE-2025-45768](https://osv.dev/vulnerability/CVE-2025-45768) | 7.0 HIGH | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인; supplier disputed, 키 강도는 운영 요구이며 면제하지 않음 | 미지정/disputed |
| pyjwt / 직접 | [CVE-2026-48526](https://osv.dev/vulnerability/CVE-2026-48526) / GHSA-xgmm-8j9v-c9wx | 7.4 HIGH | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.13.0 |
| pyjwt / 직접 | [CVE-2026-48522](https://osv.dev/vulnerability/CVE-2026-48522) / GHSA-993g-76c3-p5m4 | 4.2 MODERATE | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.13.0 |
| pyjwt / 직접 | [CVE-2026-48524](https://osv.dev/vulnerability/CVE-2026-48524) / GHSA-fhv5-28vv-h8m8 | 3.7 LOW | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.13.0 |
| pyjwt / 직접 | [CVE-2026-102268](https://osv.dev/vulnerability/CVE-2026-102268) / GHSA-ffc3-869f-jxw9 | 9.1 CRITICAL | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.14.0 |
| pyjwt / 직접 | [CVE-2026-102271](https://osv.dev/vulnerability/CVE-2026-102271) / GHSA-p4g4-x82p-q773 | 7.4 HIGH | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.14.0 |
| pyjwt / 직접 | [CVE-2026-102267](https://osv.dev/vulnerability/CVE-2026-102267) / GHSA-9v7f-9g4p-ffgj | 7.4 HIGH | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.14.0 |
| pyjwt / 직접 | [CVE-2026-102270](https://osv.dev/vulnerability/CVE-2026-102270) / GHSA-jwrc-g2q2-pq5p | 4.4 MODERATE | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.14.0 |
| pyjwt / 직접 | [CVE-2026-101917](https://osv.dev/vulnerability/CVE-2026-101917) / GHSA-2gx3-rcp4-g85q | 5.3 MODERATE | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.14.0 |
| pyjwt / 직접 | [CVE-2026-101918](https://osv.dev/vulnerability/CVE-2026-101918) / GHSA-42vr-xj54-vc7v | 5.3 MODERATE | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.15.0 |
| pyjwt / 직접 | [CVE-2026-102269](https://osv.dev/vulnerability/CVE-2026-102269) / GHSA-hxm8-2xgr-2p9m | 4.8 MODERATE | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.14.0 |
| pyjwt / 직접 | [CVE-2026-102275](https://osv.dev/vulnerability/CVE-2026-102275) / GHSA-x33g-cr3x-6449 | 6.5 MODERATE | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.15.0 |
| pyjwt / 직접 | [GHSA-pxh4-856f-4h89](https://osv.dev/vulnerability/GHSA-pxh4-856f-4h89) | 7.4 HIGH | exchange/pocket.py reauth: 서명 검증 없는 expiry metadata decode. JWKS/HMAC 검증 사용 경로 미발견; 깊이/키 강도 실제 운영은 별도 확인 | 2.14.0 |
| fastapi / 직접 | [CVE-2024-24762](https://osv.dev/vulnerability/CVE-2024-24762) / GHSA-qf9m-vfgh-m389 | 7.5 HIGH | main.py FastAPI JSON /order. Form/multipart endpoint 사용 미발견 | 0.109.1 |
| pydantic / 직접·전이 요구 | [CVE-2024-3772](https://osv.dev/vulnerability/CVE-2024-3772) / GHSA-mr82-8j83-vxmv | 5.9 MODERATE | OrderRequest/Settings/금융 DTO. EmailStr 사용 미발견 | 1.10.13, 2.4.0 |
| orjson / 직접 | [CVE-2024-27454](https://osv.dev/vulnerability/CVE-2024-27454) / GHSA-pwr2-4v36-6qpr | 7.5 HIGH | main.py ORJSONResponse 실제 dumps; 앱 직접 loads 사용 미발견 | 3.9.15 |
| orjson / 직접 | [CVE-2025-67221](https://osv.dev/vulnerability/CVE-2025-67221) / GHSA-hx9q-6w63-j58v | 7.5 HIGH | main.py ORJSONResponse 실제 dumps; 앱 직접 loads 사용 미발견 | 3.11.6 |

## 해결되지 않은 전이 의존성

| 패키지 | CVE/GHSA | 심각도 | 고정 제약/원천 사용 | 수정판 |
|---|---|---|---|---|
| h11 0.14.0 / 전이 | [CVE-2025-43859](https://osv.dev/vulnerability/CVE-2025-43859) / GHSA-vqfr-h8mv-ghfj | 9.1 CRITICAL | PocketBase 0.8.2 → httpx<0.24 → httpcore<0.17 → h11<0.15. Uvicorn fallback h11 가능, 운영 프로토콜 미검증 | 0.16.0 |
| starlette 0.37.2 / 전이 | [CVE-2024-47874](https://osv.dev/vulnerability/CVE-2024-47874) / GHSA-f96h-pmfr-66vw | HIGH (GHSA) | FastAPI 0.110.1 → Starlette<0.38. ASGI 실제 사용, 일부 Form/HTTPEndpoint/StaticFiles 경로 미발견이나 전체 악용 안전성 미입증 | 0.40.0 |
| starlette 0.37.2 / 전이 | [CVE-2025-54121](https://osv.dev/vulnerability/CVE-2025-54121) / GHSA-2c2j-9gv5-cj73 | 5.3 MODERATE | FastAPI 0.110.1 → Starlette<0.38. ASGI 실제 사용, 일부 Form/HTTPEndpoint/StaticFiles 경로 미발견이나 전체 악용 안전성 미입증 | 0.47.2 |
| starlette 0.37.2 / 전이 | [CVE-2026-48710](https://osv.dev/vulnerability/CVE-2026-48710) / GHSA-86qp-5c8j-p5mr | 6.5 MODERATE | FastAPI 0.110.1 → Starlette<0.38. ASGI 실제 사용, 일부 Form/HTTPEndpoint/StaticFiles 경로 미발견이나 전체 악용 안전성 미입증 | 1.0.1 |
| starlette 0.37.2 / 전이 | [CVE-2026-48818](https://osv.dev/vulnerability/CVE-2026-48818) / GHSA-wqp7-x3pw-xc5r | 7.5 HIGH | FastAPI 0.110.1 → Starlette<0.38. ASGI 실제 사용, 일부 Form/HTTPEndpoint/StaticFiles 경로 미발견이나 전체 악용 안전성 미입증 | 1.1.0 |
| starlette 0.37.2 / 전이 | [CVE-2026-48817](https://osv.dev/vulnerability/CVE-2026-48817) / GHSA-x746-7m8f-x49c | 5.3 MODERATE | FastAPI 0.110.1 → Starlette<0.38. ASGI 실제 사용, 일부 Form/HTTPEndpoint/StaticFiles 경로 미발견이나 전체 악용 안전성 미입증 | 1.1.0 |
| starlette 0.37.2 / 전이 | [CVE-2026-54283](https://osv.dev/vulnerability/CVE-2026-54283) / GHSA-82w8-qh3p-5jfq | 7.5 HIGH | FastAPI 0.110.1 → Starlette<0.38. ASGI 실제 사용, 일부 Form/HTTPEndpoint/StaticFiles 경로 미발견이나 전체 악용 안전성 미입증 | 1.3.1 |
| starlette 0.37.2 / 전이 | [CVE-2026-54282](https://osv.dev/vulnerability/CVE-2026-54282) / GHSA-jp82-jpqv-5vv3 | 3.7 LOW | FastAPI 0.110.1 → Starlette<0.38. ASGI 실제 사용, 일부 Form/HTTPEndpoint/StaticFiles 경로 미발견이나 전체 악용 안전성 미입증 | 1.3.0 |

- h11>=0.16을 강제로 설치하면 기존 httpcore 선언을 위반한다. --no-deps/제약 무시를 쓰지 않았다.
- Starlette>=1.3.1까지의 수정에는 현재 Pydantic v1 호환 FastAPI 상한을 넘는 프레임워크 전환이 필요하다. 공식 FastAPI 0.125.0은 Starlette<0.51, 0.126.0부터 Pydantic>=2.7.0을 요구한다. v2 migration은 이번 범위 밖이다.

## PocketBase 결정

**서버 0.16.6과 Python 클라이언트 0.8.2 모두 유지. 운영 및 릴리스 보안 게이트 BLOCK.**

| 서버 권고 | 영향 범위/수정 경로 | 실제 영향 판정 |
|---|---|---|
| CVE-2024-38351 / GHSA-m93w-4fxv-r35v | <0.22.14, password+OAuth2 linking | 0.16.6 범위 포함. 운영 auth 설정 미조회이므로 실제 악용 미확정 |
| CVE-2026-44166 / GHSA-pq7p-mc74-g65w | legacy <0.22.42 | 0.16.6 범위 포함. 운영 collection/link 정책 미검증 |
| CVE-2026-82410 / GHSA-84vh-m24q-wjjx | legacy <0.22.48, worker panic | 가용성 위험. 실제 daemon 재현/영향 미검증 |

vendor의 legacy 수정선 0.22.48이 후보이나 0.16.6→0.22.48은 단순 안전 패치라고 보장할 수 없다. 외부 일관 백업·격리 DB 복원·관리자 인증/collection CRUD·Python client API 및 Outbox/Safe Retry 보존 검증이 선행되는 별도 migration 작업이 필요하다. 이 작업에서 서버 binary를 실행/변경하거나 운영 DB에 접근하지 않았다.

## 원천 및 실행 증거

- https://pyjwt.readthedocs.io/en/stable/changelog.html
- https://fastapi.tiangolo.com/release-notes/#01101
- https://fastapi.tiangolo.com/release-notes/#01260
- https://starlette.dev/release-notes/#0372-march-5-2024
- https://github.com/ijl/orjson/releases/tag/3.11.6
- https://github.com/pocketbase/pocketbase/security/advisories
- release_checks/security_patch/resolution_before.json / resolution_final.json / resolution_delta.json
- audit_resolved_before.json / audit_resolved_final.json / findings_deduplicated.json / remaining_advisories.json

Ubuntu 실환경, 실제 PocketBase client/server 및 복원, 운영 키 권한, 실제 Discord, 512MB/203전략 부하는 모두 UNVERIFIED. STAGED_NOT_LIVE 및 OPERATING_READY=NO 유지.
