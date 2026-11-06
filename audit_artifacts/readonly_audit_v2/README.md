# POA 2단계 — 기존 체결 주문 읽기 전용 감사

**목적:** 4개 거래소의 이미 완료된 체결 주문을 인증 GET으로 조회하고, 거래소에서 **별도로 내보낸** CSV/거래내역서의 체결 행과 POA `ReadHost.observe → validate → render` 결과를 대조한다. **신규 주문, 주문 취소, DB 변경, Discord POST, 워커 기동을 하지 않는다.**

고정 제품 SHA: `bf6e66e259c7ebbd5099e78b24089caa94936ec9` (PR #4 최신 제품). 이 브랜치의 감사 및 테스트 파일만 이용한다. 제품 소스는 정규 CI에서 변경하지 않는다.

## 지원 범위

| 거래소 | 현물 | 선물 |
| --- | --- | --- |
| UPBIT | KRW 현물 | 미지원 |
| BYBIT | USDT 현물 | USDT 선형 무기한 |
| BINANCE | USDT 현물 | USDT-M |
| OKX | USDT 현물 | USDT 선형 SWAP |

Coin-M/inverse, 다른 결제통화, 만기형, 미체결·진행 중·부분취소 주문은 승인하지 않는다.

## 사전 안전조치

1. **신규 발급한 조회 전용 API 키**를 사용한다. 주문/거래/출금/이체/관리 권한은 제거하고 IP 화이트리스트를 최소화한다. 이 프로그램의 인증 조회 기능이 API 키 자체의 권한을 제한하는 것은 아니므로, 거래소 설정은 운영자가 확인해야 한다.
2. 별도 거래소 거래내역서 또는 CSV에서 **실제 개별 체결행**(`가격`, `기초자산 수량`)과 주문 집계 (`총 체결대금`, `체결수량`)를 수집한다. POA 응답을 예상값으로 복사하면 독립 대조가 성립하지 않는다.
3. 주문 ID, 거래내역서, API 키, `orders.json`을 GitHub, Actions, Discord, 이 대화에 올리지 않는다. 안전한 로컬 호스트에서만 실행한다. 실제 가격·수량·대금이 터미널에 출력되므로 터미널 로그도 비공개로 보관한다.
4. 서버/공용 PC 자동실행, Cron/Actions 비대화형 실행 금지. 운영 중인 BOT03나 실제 주문 설정에는 적용하지 않는다.

## 실행 순서 (신뢰할 수 있는 로컬 PC)

```bash
git fetch origin feat/order-fill-confirmation-v1 test/poa-readonly-audit-phase2
git worktree add --detach ../poa-historical-readonly bf6e66e259c7ebbd5099e78b24089caa94936ec9
git show origin/test/poa-readonly-audit-phase2:audit_artifacts/readonly_audit_v2/poa_readonly_audit_v2.py > ../poa-historical-readonly/poa_readonly_audit_v2.py
cd ../poa-historical-readonly
python -m venv .venv
# 운영체제에 맞게 .venv 활성화 후 실행
python -m pip install -r requirements.txt
python poa_readonly_audit_v2.py /비공개/경로/orders.json --preflight
python poa_readonly_audit_v2.py /비공개/경로/orders.json --live-read
```

**기본 모드는 없음.** `--preflight`는 SHA, 작업트리, 문서 스키마, 수치 합계만 검사하고 API/자격증명을 호출하지 않는다. `--live-read`는 대화형 TTY에서 `READ_ONLY_KEYS` 문구 입력과 거래소별 비공개 getpass를 요구한다. 비대화형 CI에서는 인증 조회를 차단한다.

**Windows 참고:** PowerShell에서 `git show ... > file.py`은 PowerShell 버전에 따라 UTF 인코딩이 달라질 수 있다. PowerShell 7이면 `git show ... | Set-Content -Encoding utf8 ..\poa-historical-readonly\poa_readonly_audit_v2.py`를 사용하고, 파일에 올바른 SHA가 기재됐는지 확인한다. Git Bash 또는 WSL Bash에서 위 명령을 그대로 사용할 수도 있다.

## 비공개 orders.json 예시 (합성 데이터, 복사해도 API 조회 불가)

```json
{
  "orders": [{
    "exchange": "UPBIT",
    "market": "spot",
    "symbol": "SOL/KRW",
    "side": "buy",
    "order_id": "REPLACE_REAL_COMPLETED_ORDER_UUID",
    "reference_source": "exchange_csv",
    "reference_fills": [
      {"price": "200000", "quantity_base": "0.25"},
      {"price": "201000", "quantity_base": "0.25"}
    ],
    "reference_total": "100250",
    "reference_quantity": "0.5"
  }]
}
```

- `reference_source`는 `exchange_csv` 또는 `exchange_statement`만 허용한다. 이 라벨만으로 자료의 진위를 증명할 수는 없다.
- 수량은 **계약 수량이 아니라 기초자산 수량**. 거래대금은 `Σ(체결가격 × 기초자산 수량)`으로 수수료 제외 금액이며 `reference_total`과 정확히 일치해야 한다.
- 선물에는 실제 주문 메타데이터의 부울형 `is_close`를 추가한다. OKX SWAP에는 실제 `contract_size`도 요구한다. 모르면 추정하지 않고 중단한다.
- 최대 100개 주문, 주문당 체결행 4,000개. 동일 거래소·시장·종목·주문 ID의 중복은 거부한다.

## 안전·성공 판정

**PASS:** 독립 원장 합계 사전 검증, 원래 POA의 인증 GET 관측, 증거 검증, 모든 체결행 가격·수량 일치, 합계·평균·종목·방향·정산통화 일치, Discord 메시지 미리보기 일치.

**FAIL/REVIEW:** 인증 오류, 외부 IP 거부, 정밀도 불일치, 일부 체결 누락, 주문 ID·종목·방향 불일치, 미완료 주문, 확인되지 않는 선물 방향, Discord 표시 불일치. 재주문 또는 재전송은 수행하지 않는다.

프로세스 수준 제한: TLS 인증서 검증을 유지하고 **공식 호스트/엔드포인트/443 포트의 GET만** 허용한다. 기타 HTTP 메서드/경로/호스트, 평문 HTTP, `requests.Session.request`를 차단한다. 이것은 OS 수준 샌드박스가 아니며 악성 코드·자격증명의 본래 거래 권한을 무효화하지 못한다.

GitHub Actions는 API 키와 실제 주문 ID 없이 합성 데이터로만 검증한다. 실계정 권한, 실제 인증 API, 실제 Discord, Vultr/BOT03는 **UNVERIFIED** 상태로 남는다. 이 도구의 성공을 운영 준비 승인으로 간주하거나 기능 플래그를 자동 활성화해서는 안 된다.


## PR #7 보안 강화 및 Ubuntu 설치 리허설 (2026-11-06)

이 문서는 PR #6의 로컬 감사 실행법을 계승하되, **PR #7의 검증된 감사 스크립트**를 사용하도록 업데이트했다. 제품은 여전히 PR #4 SHA `bf6e66e259c7ebbd5099e78b24089caa94936ec9`이며, 실운영 서버의 `main`·DB·주문 로직은 변경하지 않는다.

**파일·권한:** `orders.json`은 파일 크기 2MB 이하의 일반 파일이어야 하며, 심볼과 허용 필드가 맞아야 한다. 심볼릭 링크·명명 파이프는 금지한다. Linux에서는 입력 파일의 소유자가 현재 실행 사용자여야 하고 소유자 외 누구에게도 권한이 없어야 한다(`chmod 600`). 서버에서도 루트 계정 대신 별도 `poaaudit` 비관리자 계정을 사용한다. `umask 077` 및 `/dev/shm/poa-audit` 접근권한 0700을 권장한다.

**조회 승인:** `--preflight`는 비밀정보 입력 없이 완료되어야 한다. `--live-read`는 stdin/stdout/stderr 모두 TTY이고 `READ_ONLY_KEYS`를 수동으로 입력한 경우에만 키를 요청한다. 표준 출력을 파일이나 파이프로 전환하면 실행이 차단된다. 운영용 키나 환경변수에 등록된 키를 재사용하지 않고 신규 **조회 전용** 키를 발급한다.

**기준 원장 확장:** 거래소 원본 CSV/명세서가 수수료·포지션 정보를 제공하는 경우 다음 필드를 추가할 수 있다. 선택적 필드라도 제공했다면 불일치를 FAIL로 판정한다. 확실하지 않으면 임의로 만들지 않는다.

```json
{
  "reference_fees": {"USDT": "0.2"},
  "reference_position": "롱 청산"
}
```

- `reference_fees`: 통화별 수수료 원장. 환급·리베이트는 음수 부호가 필요할 수 있으므로 부호를 보존한다. 거래소별 수수료 부호 정의와 수수료 포함 여부가 POA의 원장 규약과 정확히 일치하는지 먼저 확인한다.
- `reference_position`: 선형 선물만 가능. `롱 진입`, `롱 청산`, `숏 진입`, `숏 청산` 중 하나.
- 출력은 의도적으로 최소화해 주문 ID와 체결 가격·수량·금액을 표시하지 않는다. Discord Embed 금액 검증 자체는 메모리 안에서 수행하며 실제 발송하지 않는다.
- 입력 CSV의 **출처 진정성은 코드가 증명할 수 없다.** 운영자가 거래소에서 별도 내보낸 자료를 확인해야 한다.

### CI 설치 리허설

`audit_artifacts/readonly_audit_v2/rehearse_ubuntu.sh`는 **GitHub Ubuntu 러너에서만** 호출한다. 임시 비관리자 계정, 별도 Python venv, 고정 제품 SHA, 비공개 합성 주문 명세, 사전 검사, 비대화형 실조회 거절을 검증한다. 실행 전후 파일·사용자를 정리한다. 실제 Vultr 계정 접속·방화벽 제어·업비트/바이비트/바이낸스/OKX 인증 호출은 하지 않는다.

해당 리허설의 의존성 설치는 패키지 인덱스에서 일반 패키지를 내려받을 수 있으므로 *완전한 네트워크 차단* 테스트는 아니다. CI에는 실제 비밀정보가 입력되지 않는다. 또한 감사 프로그램의 `http.client` 전송 가드는 **프로세스 내부 통제이며 OS 수준의 네트워크 샌드박스가 아니다**. 신뢰할 수 없는 의존성, raw socket, DNS/네트워크 경로에 대한 완전한 차단을 보증하지 않는다. 실제 읽기 전용 키는 거래소의 권한·출금/매매 제한 및 서버의 방화벽/IP 정책으로 별도 보호한다.

실서버·실거래소 검증은 **UNVERIFIED**이며 PR #7이 Draft인 동안 운영 적용·`main` 병합은 금지한다.
