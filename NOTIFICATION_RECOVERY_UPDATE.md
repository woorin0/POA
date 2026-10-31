# 매수 알림 복구 / 서버 10대 순차 업데이트

## 확인된 원인과 확인하지 않은 원인

- TradingView 신호 BUY는 `process()`가 기록 후 종료했고 admission도 SELL만 허용했습니다. BUY 전용 비금융 admission과 Outbox 발행으로 수정했습니다.
- 실제 체결 관측 BUY도 금융 provider가 없으면 기록만 남겼습니다. 완료 체결 snapshot이 있는 BUY 알림도 복구했습니다. 체결 기록 관측과 서명 검증을 구분합니다.
- startup 예외 무시는 제거했습니다. POABOT 시작 Discord 메시지는 주문 서비스 시작 인사이며 알림 Worker 성공 증거가 아닙니다.
- 서버의 실제 설정·로그·DB·Discord 응답은 조사하지 않았습니다. 매도 누락이 플래그, Worker 중단, cutover/restore gate, DB 오류, admission, Discord 오류 중 무엇인지는 아직 미확인입니다.

## 서버에서 실행할 읽기 전용 진단

실제 계정/주문/키/원문 payload를 출력하지 않는 집계 도구입니다. 운영 DB의 마이그레이션·초기화·재전송을 실행하지 않습니다.

```sh
cd /root/POA
/root/POA/.venv/bin/python scripts/notification_pipeline_status.py --db /root/POA/notifications.sqlite3
systemctl is-active poa
journalctl -u poa --since '30 minutes ago' --no-pager \
  --grep='notification_worker:|verified_alerts: WORKER_|observed_alerts: INTAKE_|verified_alerts: BUY_ADMISSION_REJECTED|verified_alerts: SIGNAL_SELL_ADMISSION_REJECTED|verified_alerts: discord_delivery_unknown|verified_alerts: RESTORE_PREFLIGHT_BLOCKED|verified_alerts: NOTIFICATION_INTAKE_REVIEW'
```

기본 User Data의 Python 경로는 `/root/POA/.venv/bin/python`입니다. 별도 설치라면 해당 `poa.service`의 실제 Python 경로를 사용하십시오. 설정 플래그만 아래처럼 확인할 수 있습니다. API 키/비밀번호/Webhook URL을 cat/print하지 마십시오.

```sh
python3 - <<'PY'
from pathlib import Path
names={'VERIFIED_DISCORD_ALERTS','DISCORD_OUTBOX_ENABLED','VERIFIED_FILL_PRICE','UPBIT_VERIFIED_ALERTS','POA_UPBIT_ESTIMATED_READ_ENABLED'}
found={}
for line in Path('/root/POA/.env').read_text().splitlines():
    k,sep,v=line.partition('=')
    if sep and k.strip() in names:
        value=v.strip().strip(chr(34)+chr(39)).lower()
        found[k.strip()]='TRUE' if value in ('true','1','yes','on') else 'FALSE' if value in ('false','0','no','off') else 'INVALID'
for name in sorted(names):print(name+'='+found.get(name,'UNSET_USE_SETTINGS_DEFAULT'))
PY
```

### 진단 코드

- `WORKER_STARTING`: 스레드 생성 요청. 준비 완료를 뜻하지 않습니다.
- `WORKER_TICK_OK`: Worker 한 사이클이 반환됨. 모든 이벤트 SENT나 외부 수신 확인을 뜻하지 않습니다.
- `WORKER_NOT_STARTED`: 필수 플래그/등록 조건 등으로 start_worker가 None 반환. `configuration_incomplete` 및 provider 경고도 확인합니다.
- `WORKER_START_FAILED`: 시작 경계 예외. 원문 예외는 출력하지 않습니다.
- `UPBIT_HOST_SETUP_FAILED` / `UPBIT_HOST_NOT_READY`: Upbit 체결조회 Host 문제. 다른 현물 신호 알림 Worker까지 막지 않습니다.
- `WORKER_STORE_INTAKE_*` / `WORKER_RECOVERY_INTAKE_*` / `WORKER_TICK_INTAKE_*`: DB 접근/복구/주기 처리 단계. `LOCKED`, `DISK_FULL`, `READONLY`, `CORRUPT`, `UNAVAILABLE` 등 안전한 범주만 출력합니다.
- `WORKER_CUTOVER_REQUIRED`, `WORKER_RESTORE_BLOCKED`: 승인/복원 대조가 필요합니다. gate를 해제하거나 DB를 삭제하지 마십시오.
- `BUY_ADMISSION_REJECTED`, `SIGNAL_SELL_ADMISSION_REJECTED`: Outbox 거부. 해당 job은 REVIEW 보존하며 임의 재전송하지 않습니다.
- `WORKER_STOPPED`, `WORKER_EXITED`: 명시적 종료/예상 밖 종료를 구분합니다.

집계 해석: Intake만 증가하면 submission/cutover/restore를 확인하고, PENDING job 정체는 Worker/due를, `OBSERVED_REVIEW`는 바인딩/admission을, PENDING delivery 정체는 sender/worker를 확인합니다. UNKNOWN/HELD는 자동 release/requeue하지 않습니다. SENT도 Discord에서 실제로 확인된 외부 전달 사실과 동일하다고 주장하지 않습니다. 프로세스 liveness와 최근 처리 여부는 systemd와 로그를 함께 확인합니다.

## 기존 서버 업데이트: 재설치 금지

이 절차는 운영자가 승인한 정비 시간에 서버별로 **한 대씩** 수행합니다. 이 작업에서 실제 서버 접속·재시작·주문·Discord 송신은 실행하지 않았습니다.

1. TradingView/외부 Webhook 유입을 운영 절차로 일시 중단합니다. 미처리 주문과 Safe Retry 상태를 확인합니다. 신호 및 주문을 수동 재실행하지 않습니다.
2. 위 읽기 전용 진단을 저장합니다. `poa.service`를 정상 정지한 후 비활성을 확인합니다. PocketBase/Caddy/다른 서버는 재설치하지 않습니다.
3. 멈춘 상태에서 `/root/POA/.env`, 모든 DB와 Sidecar 및 WAL/SHM을 **같은 시점의 전체 백업**으로 보존합니다. 디스크 여유를 먼저 확인하고 백업은 root 전용입니다.

```sh
sudo systemctl stop poa
if systemctl is-active --quiet poa; then echo 'STOP_FAILED'; exit 1; fi
umask 077
backup_dir="/root/poa-backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
tar --exclude='./.git' --exclude='./.venv' --exclude='./venv' --exclude='./__pycache__' \
    -czf "$backup_dir/poa-stopped.tar.gz" -C /root/POA . || { echo BACKUP_FAILED; exit 1; }
```

백업에는 비밀정보가 포함되므로 업로드/공유하지 마십시오. 다른 프로세스가 DB를 쓰고 있다면 중단하고 일관된 백업부터 확보합니다.

4. 최종 보고서의 main commit SHA와 manifest SHA256을 사용해 빈 임시 디렉터리에 소스 archive를 받습니다. `main` 최신값을 맹목적으로 덮어쓰지 않고 검증한 commit을 고정합니다.

```sh
release_commit='MAIN_SHA_FROM_REPORT'
release_manifest='RELEASE_MANIFEST_SHA256_FROM_REPORT'
stage_dir="$(mktemp -d /root/poa-release.XXXXXX)"
curl --fail --proto '=https' --tlsv1.2 \
  "https://codeload.github.com/woorin0/POA/tar.gz/$release_commit" -o "$stage_dir/source.tar.gz"
tar -xzf "$stage_dir/source.tar.gz" -C "$stage_dir"
python3 - "$stage_dir/POA-$release_commit" /root/POA "$release_manifest" "$release_commit" <<'PY'
import sys,json,hashlib,shutil,re
from pathlib import Path
src,dst=map(lambda p:Path(p).resolve(),sys.argv[1:3]);expected=sys.argv[3]
manifest=(src/'release_manifest_sha256.json').read_bytes()
assert hashlib.sha256(manifest).hexdigest()==expected, 'MANIFEST_MISMATCH'
entries=json.loads(manifest)
for item in entries:
    rel=Path(item['Path']);source=(src/rel).resolve();target=(dst/rel).resolve()
    assert not rel.is_absolute() and src in source.parents and dst in target.parents
    assert rel.name!='.env' and rel.suffix not in ('.sqlite','.sqlite3','.db','.env'), 'PRESERVE_RUNTIME_DATA'
    data=source.read_bytes()
    assert len(data)==item['Bytes'] and hashlib.sha256(data).hexdigest().upper()==item['SHA256'].upper(), 'SOURCE_MISMATCH'
for item in entries:
    rel=Path(item['Path']);target=dst/rel;target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(src/rel,target)
shutil.copy2(src/'release_manifest_sha256.json',dst/'release_manifest_sha256.json')
commit=sys.argv[4]
assert re.fullmatch('[0-9a-f]{40}',commit), 'COMMIT_REQUIRED'
record=dst/'.poa-deployment.json.new'
record.write_bytes(json.dumps({'commit':commit,'manifest_sha256':expected},sort_keys=True).encode())
record.chmod(0o600);record.replace(dst/'.poa-deployment.json')
print('VERIFIED_PRODUCT_FILES_UPDATED')
PY
```

기존 `.env`, API 키, 거래/알림 DB, Sidecar, 가상환경을 덮어쓰지 않습니다. `install_once.py`, cloud-init 재실행, git reset/clean, DB 삭제/초기화는 하지 않습니다. 새 의존성은 추가하지 않았습니다. 설치된 사용자가 root가 아니라면 서비스의 실제 소유권/경로를 유지하십시오.

5. 정지 상태에서 manifest 대조와 구문 검사를 완료합니다. 그 뒤 운영자가 정상 시작을 승인하고 `systemctl start poa`를 수행합니다. Safe Retry의 기존 상태/재시도 정책을 바꾸지 않습니다.
6. Worker 코드와 집계를 확인합니다. 준비 여부가 확인되기 전 Webhook 유입을 재개하지 않습니다. 임의 주문이나 실제 Discord 테스트는 이 절차에 포함하지 않습니다.
7. 첫 서버 확인 후 나머지 9대를 한 대씩 진행합니다. 신규 DB가 생성됐다는 이유로 기존 DB를 교체하지 않습니다. 문제가 있으면 신호 유입을 중지하고 정비 상태를 유지합니다.

### 롤백 주의

업데이트 후 새 주문/관측/전송이 발생한 뒤에는 과거 DB만 복원하면 최신 Sidecar/v4와 어긋나거나 전달 이력이 소실될 수 있습니다. 일괄 snapshot 복원/재발송을 금지합니다. 코드만 되돌릴지, DB/Sidecar를 외부 전달 이력과 함께 수동 대조할지 먼저 결정해야 합니다. 복원 mismatch는 기존 REVIEW/gate를 유지하십시오. 이 업데이트에서 운영 롤백 실복원은 검증하지 않았습니다.

## 표시와 남은 제한

매수 신호는 초록색, 매도 예상치는 빨간색, 손익 근거가 없는 매도는 회색입니다. KST 시각은 알림 payload 생성 시각이며 거래소 체결시각을 주장하지 않습니다. 매수 알림에는 손익/ROI를 넣지 않습니다. 가상 평균/보유량을 계산할 근거가 부족하면 미확인입니다.

TradingView 신호가 없는 ID-only 주문은 유효한 체결조회 provider가 없으면 여전히 WAIT/REVIEW로 남습니다. 요청 가격/수량을 체결 사실로 만들지 않습니다. 확인된 체결 snapshot이 있으면 매수 기록 알림을 냅니다. 원래 VERIFIED 금융 경로와 선물 알림은 변경하지 않았습니다.

기존 테스트 중 BUY는 기록만 되고 SELL POST 1건만 기대하던 count-only 기대값은 이번 의도적 BUY 복구와 다릅니다. 기존 UNKNOWN/HELD 2건의 raw head=-2 계약 예외와는 별개이며 그 원본은 변경하지 않습니다.

## 최신 종목/신호 수정

Upbit Simple Host는 BTC/XRP/LINK를 포함한 KRW 현물 주문 종목으로 선택합니다. 엄격한 VERIFIED scope는 확대하지 않습니다. 기존 시각·원가를 추측하지 않고, tv_signal이 없는 기존 JSON은 ORDER_OBSERVATION으로 남습니다. LL/Close_HL에는 공통 strategy_id가 필요합니다.

[203개 전략 공통 Webhook 템플릿·서버 지문 비교](UPBIT_MULTI_MARKET_SIGNAL_GUIDE.md)를 확인하십시오. 원본 종목 혼입 검사 4건은 기존 정상 BUY를 먼저 확인하고 잘못된 SELL의 추가 Root/Delivery/POST가 0임을 검사하도록 보완했습니다. 기존 안전 조건을 삭제하지 않았으며 UNKNOWN/HELD 원본 2건의 계약 예외와 별개입니다.

배포 기록 `.poa-deployment.json`은 commit/manifest 참조만 포함합니다. 코드/manifest가 실제 bytes와 일치하지 않으면 배포 SHA를 확정하지 않습니다. 계정 신원이나 금융 증거를 만드는 파일이 아닙니다.


## 오류 신고 Sidecar 및 신규 색상

새 이벤트만 체결 확인 BLUE `#2563EB`, 오류 RED `#DC2626`, 체결 미확인 예상 손익 PURPLE `#8B5CF6`, 일반 대기 GRAY `#64748B`를 사용합니다. 체결 확인과 금융 VERIFIED는 별도입니다. 과거 SENT/Frozen Wire를 재작성하거나 다시 보내지 않습니다.

운영 오류는 기존 `DISCORD_WEBHOOK_URL`을 이용합니다. 새 키/URL 입력은 필요하지 않습니다. 정상 주문 알림 Outbox와 분리된 `<VERIFIED_NOTIFICATIONS_DB_PATH>.ops.sqlite3`에 먼저 commit하고 독립 reporter가 전송합니다. Worker가 시작되지 않아도 reporter는 동작할 수 있습니다. 이 경로는 고정된 비금융 장애/복구 양식만 허용하며 주문/금융 payload를 받지 않습니다.

- 같은 서버·코드·주문 지문·단계의 열린 장애는 한 번만 기록합니다. 복구는 별도 사건이며 검증된 복구 후 재발은 새 episode입니다. 주문 처리 결과가 미확정인 장애를 단순히 시간이 지났다는 이유로 복구 처리하지 않습니다.
- reporter 전체 최대 6회/분, 명시적 429만 최대 3회 재시도합니다. Timeout/5xx/POST 중 중단은 UNKNOWN, 명시적 거부는 HELD로 보존하며 자동 재발송하지 않습니다.
- Worker tick의 짧은 일시 오류는 5초 유예 중 복구되면 보내지 않습니다. 정상적인 체결 대기를 오류로 발송하지 않습니다.
- 오류 코드·단계는 관측된 범주입니다. 포괄적인 `UPBIT_OBSERVATION_REVIEW`만으로 인증/IP/거래소 장애를 단정할 수 없습니다. 원문 예외·주문 JSON·계정 참조·키·JWT·Webhook URL을 출력하지 않습니다.
- 오류 송신 실패는 다른 오류 송신을 유발하지 않습니다. Discord 장애, 프로세스 전체 중단, 디스크 고장/권한 오류로 Journal commit이 불가능하면 실제 수신은 보장할 수 없습니다. 이 경우 비밀정보 없는 고정 로컬 코드만 기록됩니다. systemd·디스크·외부 모니터링은 별도입니다.

위의 정지 후 전체 백업에 **ops Sidecar 및 WAL/SHM도 포함**하십시오. Sidecar만 삭제하거나 과거 버전으로 복원하면 중복 억제 근거가 사라집니다. 알림을 재시도하려고 파일을 삭제하지 마십시오. 기존 `.env`, 거래소 키, Intake, Outbox, 금융 DB 및 전략 자료는 보존합니다.

다음은 배포 후 운영자가 수행할 읽기 전용 진단 예시이며, 개발 중 실서버에서 실행하지 않았습니다.

```sh
cd /root/POA
git rev-parse HEAD
systemctl is-active poa
/root/POA/.venv/bin/python scripts/notification_pipeline_status.py --db /root/POA/notifications.sqlite3 --events 40 --server-alias BOT03
/root/POA/.venv/bin/python - <<'PYREAD'
import sqlite3
from pathlib import Path
path=Path('/root/POA/notifications.sqlite3.ops.sqlite3')
if not path.is_file():
    print('OPS_JOURNAL_NOT_PRESENT')
else:
    c=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        for row in c.execute('SELECT code,phase,status,count(*) FROM ops_events GROUP BY code,phase,status LIMIT 40'):
            print(*row)
    finally:c.close()
PYREAD
```

DB 경로는 해당 서버의 실제 설정과 일치시켜야 합니다. 원문 행/UUID/계정 정보는 조회하지 않습니다. BOT03 한 대부터 기존 정지→동시점 백업→fast-forward 업데이트→manifest 검증→운영자 시작 순서로 적용하고, 상태를 확인한 뒤 나머지 서버를 순차 적용하십시오. 이번 작업은 서버 배포·재시작·실거래·실제 Discord 송신을 포함하지 않습니다.
