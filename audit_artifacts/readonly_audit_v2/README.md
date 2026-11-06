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
