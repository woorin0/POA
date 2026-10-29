# Upbit ESTIMATED 간편 설치 — Ubuntu 22.04

## 입력은 기존 항목만

최신 `deployment/cloud-config-ubuntu2204.yaml`에서 다음 값만 준비한다.

- domain
- credentials.PASSWORD (Webhook 비밀번호)
- credentials.DISCORD_WEBHOOK_URL
- 사용하는 거래소의 API Key/Secret 및 기존에 필요한 passphrase

account_uuid, pocket_uuid, strategy, allowed_ips, evidence를 **일반 설치 입력으로 요구하지 않는다**. repo_ref=main과 DB 관리 계정 기본값 등 기존 내부 설치 설정은 유지된다. 실제 값이 채워진 User Data/.env는 비밀 자료이므로 GitHub/채팅에 올리지 않는다.

Upbit Key/Secret 두 값이 유효한 문자열이면 installer가 다음 값을 내부 기본 설정으로 생성한다:

```dotenv
POA_UPBIT_ESTIMATED_READ_ENABLED=true
POA_UPBIT_READ_CREDENTIAL_MODE="simple"
```

별도 binding JSON·pin·Windows Credential Manager가 필요하지 않다. 키가 없거나 읽기 client 형식에 맞지 않으면 해당 추정 조회 설정은 비활성으로 남는다. 기존 설치 프로그램의 필수 입력·거래소 credential 쌍 검증은 그대로다.

## 실제 동작

`main.order → main.log → Intake → Worker → GET /v1/order → ESTIMATED → Outbox`를 사용한다. 주문 실행·수량·Safe Retry는 변경하지 않는다. Host를 만드는 것만으로 GET하지 않는다.

- 계정 키는 기존 POA Settings.UPBIT_KEY/UPBIT_SECRET을 명시된 simple 모드에서 재사용한다. 키 자체를 조회 전용 키로 바꾸는 기능이 아니다.
- 요청은 고정 HTTPS Upbit host의 GET /v1/order만 허용하는 기존 JWT client다. 주문·취소·출금·권한 변경 API는 제공하지 않는다.
- 조회 직전에 저장된 POA job의 유효 lease, Intake 원문, submission ACK, account/source ref, 주문 UUID, 종목, 원래 strategy_id/order_name 연결을 검사한다. 임의 UUID/flag/callback으로 조회 권한을 만들지 않는다.
- 기존 POA producer와 동일한 source_account_ref를 사용한다. 내부 account 이름은 `poa-estimated-upbit:`로 시작하며 내부 pocket 참조도 POA용이다. 실제 업비트 공식 계정/포켓 UUID 또는 금융 VERIFIED 증거라고 주장하지 않는다.
- 원래 strategy_id가 있으면 이를 사용하고, 없으면 order_name을 사용한다. 별도 수동 전략 등록은 없다. 서로 다른 전략의 원가는 합치지 않는다.
- 이름이 비어 있거나 `default`/`주문`처럼 귀속을 구별할 수 없는 기본 이름이면 숫자를 만들지 않고 UNAVAILABLE로 표시한다. 여러 전략은 서로 구별되는 실제 식별자를 주문에 제공해야 한다.
- 키가 바뀌면 source_account_ref도 바뀐다. 과거 키의 원가 기록을 새 키의 계정으로 임의 병합하지 않는다.

## 손익 의미와 제한

ESTIMATED는 저장된 POA 매수/매도 관측의 추정값이다. 거래소 원가 원장이나 독립 HISTORY가 완전하다는 의미가 아니다.

- 실제 완료 체결의 수량·VWAP만 사용한다. 주문 요청 가격, 시장가 매수의 KRW 총액을 체결 단가로 사용하지 않는다.
- 확인된 매수 관측과 충분한 수량이 있을 때만 매도 수량에 해당하는 이동가중평균 원가를 배분한다. 부분매도 후 남은 원가를 중복 차감하지 않는다.
- 매수 기록 없음, 관측 재고 초과 매도, 과거 보유분의 원가 불명, 전략 귀속 불명은 UNAVAILABLE다. 실제 외부 입출금/수동 거래/공유 재고까지 증명한 결과가 아니다.
- paid_fee를 확인하면 주문 단위 비용으로 한 번만 반영한다. 누락은 0으로 가정하지 않고 '수수료 미반영 추정 차익'으로 표시한다.
- 현재 native Upbit 조회 지원 범위는 기존과 같은 **KRW-BTC/BTC/KRW 현물**이다. 다른 종목을 섞거나 지원한다고 표시하지 않는다.
- 예시 Mock: 매수100,000,000원+fee50,000원, 매도110,000,000원-fee55,000원 → 추정 +9,895,000원, 약+9.89%. 실계정 검증값이 아니다.

실제 키의 주문조회 권한 및 허용 IP는 운영자가 Upbit에서 확인해야 한다. 자동으로 권한/IP가 등록됐다고 주장하지 않는다. 401/403이면 기존 client가 이후 인증 요청을 차단하며 해당 관측은 REVIEW로 남긴다. timeout/429/5xx는 기존 제한된 재시도 정책만 사용한다. 조회 실패가 주문 재실행을 유발하지 않는다.

## 유지되는 안전 계약

금융 VERIFIED receipt/서명/취득원가/HISTORY/FeeAudit/strict v5 scope는 변경하지 않는다. 금융 VERIFIED가 실제로 유효하면 우선한다. 간편 모드는 EXECUTION/HISTORY/ACCOUNTING/FINANCIAL 영수증을 발급하지 않는다.

기존 Outbox frozen wire·lease·begin_post·ACK·CAS, SENT 불변, UNKNOWN/HELD 무승인 전송 금지, 동일 사건 중복 차단을 유지한다. 기존 수동 UNKNOWN/HELD2 예외는 이번에 변경하거나 PASS로 재분류하지 않는다.

기존 Linux 고급 binding 모드(`poa_settings`)와 Windows vault 모드는 별도 경로로 보존한다. 간편 설치가 그 금융 검증 조건이나 기존 고급 입력 자료의 의미를 바꾸지 않는다.

## 기존 설치의 설정 변경

새 서버용 install_once.py를 기존 운영 DB 위에서 재실행하지 않는다. 승인된 정지·백업·업데이트 절차 후, 간편 모드를 사용하려는 운영자는 기존 .env에 위 두 값을 설정한다. 키는 기존 .env의 Settings에서 취득한다. 기존 binding file/pin은 simple 모드에서 사용하지 않는다.

```sh
# 안내용. 이번 코드 작업에서는 실행하지 않았다.
sudoedit /root/POA/.env
# 별도 운영 승인 후에만:
sudo systemctl restart poa
```

비활성화는 POA_UPBIT_ESTIMATED_READ_ENABLED=false로 설정한 후 승인된 재시작 절차를 따른다. systemd WorkingDirectory=/root/POA, Pydantic Settings의 .env 읽기와 명시적 process flag 우선순위는 기존 Ubuntu 지원 방식이다. 비밀키를 systemd unit/명령 인자에 넣지 않는다.

이번 작업은 Mock HTTP/Discord·격리 SQLite 검증과 GitHub 소스 업데이트만 수행한다. 실계정 API/주문/Discord/운영 서버/운영 DB는 사용하지 않는다. 실제 Ubuntu 22.04/Vultr 기동과 실제 조회 성공은 UNVERIFIED다.
