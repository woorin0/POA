# Ubuntu 22.04 Upbit ESTIMATED 활성화

이 문서는 코드 설정 안내다. 이번 변경에서 서버 설치·재시작·실계정 API·Discord를 실행하지 않았다.

## 신규 설치: Ubuntu User Data

최신 `deployment/cloud-config-ubuntu2204.yaml`을 사용한다. 기존 GitHub main fetch/manifest 검증/Python3.10 venv/PocketBase/Caddy/systemd/주문·Safe Retry 설정은 유지된다. User Data는 실제 값을 채우면 비밀 자료이므로 공개 저장소/채팅에 올리지 않는다.

기본 `upbit_estimated_read.enabled=false`다. 키가 존재해도 자동으로 조회를 활성화하지 않는다. 기존 credentials.UPBIT_KEY/UPBIT_SECRET을 사용하려면 명시적으로 아래 등록 정보를 제공한다:

- enabled: true
- credential_mode: poa_settings
- account_uuid / pocket_uuid: 승인된 계정·포켓 매핑의 canonical UUID
- strategy: 실제 webhook/POA strategy_id와 같은 등록 전략 한 개
- permissions: order_query 필수. 기존 거래 키면 order가 함께 있을 수 있음
- allowed_ips: 운영자가 Upbit 키 관리에서 확인한 실제 접속 IP 목록
- evidence: source/reference/digest/expires_at(숫자 UTC Unix seconds)

키의 주문조회 권한과 접속 IP를 운영자가 확인해야 한다. 임의 UUID를 생성하거나 이를 거래소가 인증한 계정 UUID라고 표현하지 않는다. 기존 승인 매핑이 없다면 비활성으로 남기고 매핑부터 확보한다. installer는 UUID를 생성하지 않는다. evidence.digest는 실제 운영자 검토 자료의 SHA256이며 임의 문자열을 승인 근거로 사용하지 않는다. 만료는 현재 이후·366일 미만이다.

`poa_settings`는 **기존 거래용 키를 조회에 명시적으로 재사용**한다. 키 자체가 조회 전용으로 바뀌는 기능이 아니다. 이 client의 요청은 고정 HTTPS GET /v1/order로 제한되고 POST/주문/취소/출금 기능을 제공하지 않는다. 다른 키 권한이 실제 비활성인지 자동 검사했다고 주장하지 않는다. 별도 조회 전용 키 모드는 이번 Linux 최소 범위에 포함하지 않았다.

installer가 추가로 수행하는 일:

1. 입력 binding·권한 기록·기존 키의 형식을 검증한다. 잘못된 enabled=true 설정은 안전한 코드로 설치를 중단한다.
2. 실제 주문 producer와 동일한 `identity('UPBIT', UPBIT_KEY)` source-account ref 및 조회 key fingerprint를 산출한다. 키·secret은 binding에 저장하지 않는다.
3. `/etc/poa/upbit-notification-binding.json`을 root 소유0600, 상위 디렉터리0700으로 새로 생성한다. 기존 파일을 덮어쓰지 않는다.
4. 기존 `/root/POA/.env`에 아래 네 설정을 기존 key/secret과 함께 기록한다. 금융 VERIFIED 플래그나 Safe Retry를 임의 활성화하지 않는다.

```dotenv
POA_UPBIT_ESTIMATED_READ_ENABLED=true
POA_UPBIT_READ_CREDENTIAL_MODE="poa_settings"
POA_UPBIT_READ_BINDING_FILE="/etc/poa/upbit-notification-binding.json"
POA_UPBIT_READ_BINDING_SHA256="<생성된 binding 원문 SHA256>"
```

값을 채운 User Data를 실제로 실행하는 것은 운영 설치 작업이며 별도 승인 대상이다. 이번 작업은 템플릿만 수정한다.

## 기존 Ubuntu 설치

기존 DB/sidecar를 지우거나 새 서버용 install_once.py를 재실행하지 않는다. 먼저 별도 승인된 정지·백업·코드 갱신 절차가 필요하다. 기존 Windows BindingV1 파일을 Linux 스키마로 자동 재해석하지 않는다.

현재 installer의 순수 함수 `upbit_read_configuration(data, values)`가 Linux binding JSON과 네 env 설정을 생성하는 기준 계약이다. values는 기존 POA Settings에 설정된 UPBIT_KEY/UPBIT_SECRET이며 코드·명령 인자에 키 원문을 쓰지 않는다. 이 함수를 운영에서 실행하는 준비 도구/명령을 이번에 자동 설치하지 않았다. 새 서버용 설치 또는 승인된 안전한 설정 절차로 생성한 Linux binding이 필요하다. binding pin만 임의 추가해서는 READY가 되지 않는다.

Linux binding 최상위 필드는 format/config/evidence/permissions/key_usage/allowed_ips이며 format=POA_UPBIT_LINUX_BINDING_V1, key_usage=POA_SETTINGS_SHARED_TRADING_KEY다. config는 기존 UpbitReadConfig이고 installer가 source-account ref/read key fingerprint/producer를 고정한다. 원문 hash와 file 권한이 맞아야 하며 매 조회 직전 다시 검증한다.

운영자가 승인한 편집 절차로 .env와 binding을 준비한 뒤에만 서비스를 새 설정으로 시작한다. 예시 명령은 안내이며 이번에 실행하지 않았다:

```sh
sudoedit /root/POA/.env
# 실제 운영 승인·설정 검토 후에만:
sudo systemctl restart poa
```

.env나 binding 내용을 터미널에 cat하거나 로그에 남기지 않는다. 키는 이미 로드된 Settings에서만 취득한다. Linux에서 Windows Credential Manager 또는 ctypes.WinDLL을 호출하지 않는다. Windows에서는 기존 vault 공급자 경로를 유지한다.

## 설정 전달 / 제한

네 필드는 Pydantic Settings에 등록되어 .env가 실제로 읽힌다. systemd WorkingDirectory=/root/POA에서 기존 dotenv 경로를 사용한다. 단순히 os.environ만 읽는 구조가 아니다. 명시적 프로세스 환경의 flag/pin이 있으면 그것을 우선하고, 없으면 Settings에서 해석한 .env 값을 쓴다. systemd EnvironmentFile로 .env를 다시 해석하지 않는다. JSON 문자열/리스트의 shell 인용 혼동을 피하고 키를 unit 파일에 쓰지 않는다.

단일 Host는 현재 **한 계정·한 포켓·KRW-BTC·한 등록 전략**만 지원한다. 다른 계정/전략/종목/주문은 혼합하지 않는다. 여러 전략이면 현재 지정한 전략 하나만 이 조회 Host를 사용할 수 있다. 나머지는 WAIT/REVIEW 등 기존 안전 경로로 남으며 자동 등록하지 않는다.

완료 체결의 수량·VWAP·paid_fee를 기존 엔진으로 처리한다. BUY 관측 없으면 UNAVAILABLE, fee 없으면 수수료 미반영 차익이다. 미체결/응답 모순/조회 권한 오류는 금융 확정이나 강제 재전송으로 처리하지 않는다. 독립 HISTORY/FeeAudit/FINANCIAL VERIFIED를 생성하거나 우회하지 않는다. 기존 VERIFIED 우선, SENT 불변, UNKNOWN/HELD 승인 조건 및 wire/lease/CAS/ACK를 유지한다.

root/서비스 사용자 소유의 regular file,0600 이하,hardlink1,8192bytes 이하 및 pinned SHA를 확인한다. 코드가 확인하는 것은 보호된 운영자 등록과 key/source-ref 일치다. 실제 계정 소속·전략 소유권·키 권한 전체·실제 egress IP의 외부 진위 확인은 운영자의 책임이며 GET 성공만으로 별도 VERIFIED identity를 주장하지 않는다.

## 검증 범위

Windows에서 Linux/POSIX 분기와 os.open/fstat/NOFOLLOW 정책을 Mock으로 검증했다. WSL/Ubuntu distro 및 Docker가 없어 Ubuntu22.04 실제 import/startup은 수행하지 못했다. 실제 Vultr/실계정/Discord·운영 DB·512MB VPS 부하는 모두 UNVERIFIED다. 코드의 Mock 통과와 운영 준비를 구분한다.

공식 자료: [Pydantic .env 설정](https://docs.pydantic.dev/1.10/usage/settings/), [Python os.open](https://docs.python.org/3.10/library/os.html#os.open), [Upbit 주문 조회](https://docs.upbit.com/kr/reference/get-order).

OPERATING_READY=NO. 실제 거래소·서버 테스트는 별도 승인 후 진행한다.
