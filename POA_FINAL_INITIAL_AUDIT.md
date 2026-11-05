# 최종 릴리스 최초 감사 / 수정 범위

기준: PR4 e09c329f882dcfca55a747c21f86dd53c231a721.
main de969040, PR3 77ec5dab 및 PR4 원본은 보존한다.
이 문서는 실행하지 않은 설치·운영 검증을 PASS로 주장하지 않는다.

| 단계 | 초기 판정 | 근거 / 필요한 수정 | 검증 |
|---|---|---|---|
| Python/의존성 | PASS (기존 CI) | 양 OS freeze 감사 0건, requirements 유지 | 최종 SHA 설치/pip check/audit |
| 앱 실제 시작/종료 | UNVERIFIED | HTTP 테스트와 실제 프로세스 시작은 구분 | 새 Ubuntu VM 실제 systemd/HTTP/재부팅 |
| Pydantic/인증/Whitelist/주문 전달 | PASS (기존 회귀) | 변경 금지 | 기존 HTTP/Golden/Safe Retry |
| PocketBase SDK/서버 | UNVERIFIED (최종 Linux 설치) | SDK 0.12.2/서버 0.22.48 합성 실험 있음; 기존 0.16.6 DB 이관 금지 | 새 DB 관리자/collection CRUD/실제 POA |
| 거래소별 설정 | UNVERIFIED (실계정) | 실제 키를 읽지 않음; 미사용 키는 공란 허용 | 합성 설정/실계정은 별도 권한 필요 |
| 4거래소 체결금액 | PASS (합성 292건) | 7시장/BTC ETH SOL/BUY SELL/단일 복수; 제품 재개발 안 함 | 최종 SHA 독립 E2E/전체 CI |
| 증거/Outbox/ACK/CAS/복구 | PASS (기존 안전 회귀) | 기존 전송/금융/주문 원문 불변 | 실제 검증기/서명/멱등성 회귀 |
| 오류 재발 테스트 Probe | FAIL | 동일 wire digest의 다른 episode를 첫 행으로 오선택; test_alert_policy.py의 현재 POSTING/후보수/token 결합 보완 | 동일 digest/다른 episode 독립 반례; 기대 3건 유지 |
| Ubuntu installer 실행 경로 | FAIL | install_once.install은 Ubuntu를 STAGED_NOT_LIVE로 라우팅 | 새 서버 전용 production 경로; Debian/기존 staging 보존 |
| 고정 설치 소스 | PASS (현재 72제품) | 제품 B 150ea89와 현재 PR4 제품 bytes 일치. SHA가 오래됐다는 이유만으로 불일치로 판단하지 않음 | 최종 제품/Manifest/User Data 비순환 B/C 고정 |
| 설치 Git 이력 | FAIL | stage fetch --depth 1과 git-show 기반 보호 게이트 충돌 | 필요한 기준 이력 확보 후 보호 검사 |
| systemd/Caddy/공개 HTTPS | UNVERIFIED | 현 Ubuntu User Data는 서비스 자동 시작을 의도적으로 차단 | 새 격리 VM 실제 실행; DNS/외부 인증서는 별도 실측 |
| 신규 VM 환경 | UNVERIFIED | 로컬 Windows: 가상화 OFF, WSL 없음 | 먼저 GitHub 격리 runner의 KVM/Ubuntu VM 실행 가능성 확인 |
| 실계정 체결값/Discord 실제 수신 | UNVERIFIED | 실계정/도메인/새 VPS를 제공받지 않음 | 권한 없이 접근/송신하지 않음 |

## 통합 수정 계획 (이 감사 후 변경 범위)

1. 실제 Ubuntu cloud-init 부팅 시험이 가능한 환경을 먼저 확인한다. 불가능하면 미검증 production User Data를 만들거나 배포하지 않는다.
2. Probe는 현재 POSTING 후보를 유일하게 고르고 wire/digest/token을 확인한다. 과거 SENT/동일 episode 안전 검사는 유지한다.
3. 기존 staging/검증기/신뢰 계약을 재사용하는 새 서버 전용 production 설치 경로만 추가한다. 빈 서버, 명시적 승인, 고정 SHA/전체 Manifest, 비밀정보 보호, 정상 중단, Caddy 경계, 기존 데이터 덮어쓰기 금지를 우선한다.
4. 거래·인증·수량·Safe Retry·금융·ACK/CAS는 수정하지 않는다. 실제 서비스/주문 활성화와 설치 완료 상태를 분리한다.
5. 집중 검사 후 전체 회귀 1회, 실제 Ubuntu VM 설치/프로세스/재부팅, 최종 Windows/Ubuntu CI를 수행한다. 통합 검증 실패 시 원인별 최소 보완 1회만 허용하며 재실패하면 REJECTED다.
6. 최종 Manifest는 실제 bytes로 생성하고 User Data는 그 제품 커밋을 별도 고정한다. 검증된 파일만 배포용으로 표시한다. 기존 서버 및 실제 주문·API·Discord는 접근하지 않는다.

외부 근거: [TradingView 공식 웹훅](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/), [GitHub runner](https://docs.github.com/en/actions/reference/runners/github-hosted-runners), [cloud-init NoCloud](https://docs.cloud-init.io/en/latest/reference/datasources/nocloud.html), [PocketBase 0.22.48](https://github.com/pocketbase/pocketbase/releases/tag/v0.22.48).
