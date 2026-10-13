# Ubuntu 22.04 설치 오류 복구

원인: 사진의 서버는 Ubuntu 22.04지만 기존 설치기는 Debian 12만 허용했습니다.
`POA_INSTALL_FAILED=DEBIAN_12_REQUIRED`는 POA 소스와 DB를 만들기 전 검사에서 발생합니다.

## 현재 실패한 서버

1. `recover-ubuntu2204.py`를 SFTP/WinSCP로 서버의 `/root/recover-ubuntu2204.py`에 올립니다.
2. 서버 콘솔에서 실행합니다.

```bash
sudo python3 /root/recover-ubuntu2204.py
echo "POA_RECOVERY_EXIT_CODE=$?"
```

기존 `/root/poa-install-input.json`의 도메인과 인증정보를 그대로 사용합니다.
기존 설치 보조 파일은 `/root/poa-installer-backup-*`에 백업합니다.
`/root/POA`, `/root/db`, 설치 완료 기록 중 하나라도 있으면 데이터 보존을 위해 중단합니다.
이 경우 폴더를 삭제하거나 cloud-init 전체를 재실행하지 마세요. 중단 코드와 파일 존재 여부부터 점검해야 합니다.
입력 파일이 없는 경우에도 중단합니다. 인증정보를 공개 채팅이나 GitHub에 올리지 마세요.

복구는 Ubuntu 기본 Python 3.10으로 독립 venv를 만들고 기존 requirements.txt를 그대로 설치합니다.
Snap Caddy가 있으면 해당 서비스만 중지/자동시작 해제하며 데이터는 삭제하지 않습니다.
Caddy 공식 APT 저장소의 systemd 서비스를 사용합니다.
공식 설치 근거: https://caddyserver.com/docs/install#debian-ubuntu-raspbian

## 새 서버

Vultr에서 **Ubuntu 22.04 amd64**를 선택하고 `cloud-config-one-shot-ubuntu2204.yaml` 전체를 User Data에 사용합니다.
상단 JSON의 도메인, 웹훅 비밀번호, Discord URL, 사용할 거래소 키/Secret을 채우세요.
이 파일은 서버 콘솔에 붙이는 Bash 명령이 아닙니다. Debian 12에는 별도의 Debian 파일을 사용하세요.

두 방법 모두 이전에 제공한 GitHub 배포 ZIP의 **내용물**이 저장소 루트에 올라가 있어야 합니다.
`release_manifest_sha256.json`과 소스 해시가 맞지 않으면 설치는 중단됩니다.
도메인 DNS가 서버 IP를 가리키고 외부 방화벽에서 80/443을 허용해야 HTTPS 준비가 완료됩니다.
성공하면 서비스가 자동 시작되어 유효한 TradingView 주문을 처리할 수 있습니다.

## 성공 확인

`POA_INSTALL_SUCCESS`와 실제 종료 코드 0을 모두 확인하세요.

```bash
sudo systemctl is-active pocketbase poa caddy
sudo cat /root/POA_INSTALL_RESULT.json
```

세 서비스가 active인지 확인합니다. 복구 성공 뒤에도 이전 cloud-init 실패 기록은 남을 수 있습니다.
검증을 위해 주문 웹훅을 전송할 필요는 없습니다.

로컬 Mock 테스트 20/20 통과. 실제 Linux 패키지 설치, Vultr 서비스 시작, 인증서 발급,
Python 3.10 전체 앱 회귀 및 512MB 부하는 미검증입니다. 서버 설치 성공을 사전에 보장하지 않습니다.
