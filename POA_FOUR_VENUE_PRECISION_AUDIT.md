# POA 4대 거래소 알림·신호 손익 정밀 감사

RESULT: PARTIAL (기능 검사 통과, 원본 source-pin 2 FAIL 및 실서버 미검증 보존)
BASE_SHA: 8e4f8acb12956ca0df6df089700e700f9948a4ef
범위: 현물 TradingView 신호 추정치. 실제 거래소 체결·정산손익 검증이 아니다.

## 실행과 독립성

별도 전체 detached checkout `D:/Antigravity/POA-four-venue-precision-audit`에서 실행했다.
원본 작업공간은 변경하지 않았다. 시작 제품 manifest 67개를 확인하고, 실제 로드된 제품 모듈 경로와 raw SHA256을 기록했다.
실제 `/order`와 `/` route, main.log 및 schedule_order_log의 소스 AST를 그대로 로드한 FastAPI 앱에 httpx ASGITransport로 JSON을 입력했다. main의 운영 시작 부작용은 실행하지 않았다.
get_bot의 거래소 주문 경계만 Fake Order Adapter로 대체했다. 요청 주문 가격 777·수량0.002와 신호가격·수량을 다르게 넣어 두 경로의 독립성을 확인했다.
계산·producer 검증·Intake·Worker tick·admission·직렬화·중복 제거·DB ACK는 실제 구현이다. 물리적 requests.post만 Mock으로 차단했다.
소켓/DNS 및 운영 SQLite 접근 guard를 유지했고, 테스트용 임시 SQLite만 사용했다. 라이브 API/주문/Discord/서버 접근은 0이다.
운영자가 실제 수신한 Webhook JSON은 제공되지 않았으므로 그 실측은 UNVERIFIED다. 정보가 모두 있는 공통 JSON 계약에서 내부 결함을 재현했다.

## 최종 검증

|대상|BUY|SELL|PNL|ROI|
|---|---|---|---|---|
|UPBIT BTC/KRW, XRP/KRW, LINK/KRW|PASS|PASS|PASS|PASS|
|BINANCE BTC/USDT, XRP/USDT|PASS|PASS|PASS|PASS|
|BYBIT BTC/USDT, XRP/USDT|PASS|PASS|PASS|PASS|
|OKX BTC/USDT, XRP/USDT|PASS|PASS|PASS|PASS|

- 신규 독립 감사: 65 PASS / 0 FAIL / exit0 (`final.xml`, 148.17초).
- v2 Intake 원문·digest 재시도 및 오래된 v4 snapshot 복원 차단: 2 PASS / 0 FAIL / exit0 (`restore_compat.xml`).
- 영향 회귀: 99 PASS / 1 source-pin FAIL / exit1 (`regression.xml`).
- 원본 다른 source-pin: 0 PASS / 1 FAIL / exit1 (`original_pin.xml`).
- Safe Retry 대표 검사: 7 PASS / 0 FAIL / exit0 (`safe_retry.xml`, 독립 프로세스).
- 최종 고유 기능 검사 173 PASS; 원본 source-pin 2 FAIL을 PASS로 바꾸지 않았다. 재실행 수를 합산하지 않았다.
- 원본 UNKNOWN/HELD raw head=-2 기대 2건은 승인된 과거 기준선 예외다. 이번 검사에서 재실행하거나 기대값을 변경하지 않았다. 새 UNKNOWN/HELD 보호 검사와 혼합하지 않는다.

증거는 로컬 `audit_artifacts/precision_audit/`의 JUnit XML/log/product_manifest.json/summary.json에 있다. 테스트 SQLite·로그·임시 결과는 Git에 올리지 않는다. 재현 테스트 소스만 포함한다.

## 확인된 결함과 최소 수정

수정 전 `before.xml`: Oracle 6 PASS / 13 FAIL. `before_delta.xml`: 추가5 FAIL.

|결함|재현 입력·잘못된 출력|수정·최종 근거|
|---|---|---|
|신호 ID 소실|top/nested signal_id가 있는 실제 JSON → DTO.signal_id=None|Intake에서 원래 ID 보존, 신규 snapshot→payload 및 admission에 결합. 9시장 E2E와 nested-only 검사 PASS|
|신호 시각 소실|signal_time=1790812800 → Intake에 없음|옵션 시각 보존·일치 검증, 신호 KST와 알림 생성 KST 구분. 2026-10-01 09:00:00 KST 확인|
|ID 모순 승인|top ID와 nested ID가 다름 → 주문 경계까지 진행|알림 JSON binding validator에서422, Fake 주문 호출0|
|소량 수량 0 표시|0.000000001 BTC → 0 BTC|신규 알림 수량 Decimal 표시 보존|
|큰 값 알림 유실|허용된1e24 수량 또는 큰 ROI → quantize 오류로 Outbox 없음|알림 표시만 충분한 Decimal context 사용, 양쪽 BUY/SELL POST·ACK 확인|
|긴 전략명 잘림|허용된 전략명64자 이후 식별 부분 소실|허용된128자 범위 표시, 한글·모바일 길이 검사|
|청산 후 보조원가 미적용|전량청산 후 명시적 strategy.position_avg_price=150, 매도160 → UNAVAILABLE|신규 알림 계산에서 청산 사이클 종료 반영, 추정10. 과거 계산 버전 유지|
|명시적 역순 신호 연결|t2 SELL→t1 BUY→t3 SELL → 잘못된 추정20|새 계산에서 감소하는 원천 시각 탐지, TV_EVENT_ORDER_UNCERTAIN/UNAVAILABLE. 과거 사건 재정렬·재발송 없음|

중간 구현의 문법 오류 실행과 203전략 시험의 잘못된 마지막-revision 선택도 로그에 보존했다. 후자는 bounded Worker 큐가 남은 상태에서 별개 BUY를 읽은 시험 관측 오류였으며, due/state 변경 없이 실제 Worker tick으로 큐를 처리한 뒤 검증했다. 제품 안전 조건을 약화하지 않았다.

## 수학·격리·표시

독립 Decimal Oracle은 제품 계산 함수를 사용하지 않는다. 기준 A~F는 수정 전부터 모두 PASS였다.
A +10,000,000 KRW/10%; B +50,000 KRW/10%; C +200,000 KRW/12.5%; D +18,666.666… KRW/21.875%; E +56 USDT/13.207547…%; F -100/-10%.
12회 분할매수, 여러 부분매도, 전량 종료·재진입, 잔여 원가, 초과 매도의 매칭/미매칭 수량, 1e-9 수량, 큰 값, 손실, 0/음수/NaN/Infinity 거부를 확인했다.
203개 전략의 별도 영속 scope와 표본 청산을 확인했다. LL/Close_HL은 같은 strategy_id로 연결되며, 다른 API-account ref·전략·종목은 섞이지 않는다. 가짜 장기 보유 잔고·계정 평균매수가를 넣어도 원가에 쓰이지 않는다.
기존 관측 수수료 누락/제3통화/중복 비용 회귀와 신호 계산의 수수료 미반영 정책은 유지했다.
원 계산은 Decimal로 비교하며 KRW/USDT 가격·손익·ROI는 기존 표시 반올림 정책을 유지한다. 수량은 새 알림에서 0으로 뭉개지지 않는다.
Frozen Wire의 종목·전략·수량·금액·ROI·KST·색상(16A34A/EF4444/64748B)·문구·mentions 차단과 모바일 제한을 검사했다. 매수에는 PnL/ROI가 없고, 신호를 체결 완료/VERIFIED로 표현하지 않는다.

## 전달·복구·동시성

200/204는 기존 ACK 규칙상 SENT(204는 message ID가 없을 수 있음). 400/401/404는 FAILED, 429는 Retry-After=2초 이후에만 제한 재시도, 500/502/503·timeout·연결 실패는 UNKNOWN을 유지했다.
정상 사건은 POST1, 재시작·중복 처리 후 추가POST0. 429만 명시적 거절 뒤 예정 재시도가 허용된다.
실제 data=frozen_bytes, SHA256, root/revision/delivery, epoch/token hash, attempt 및 DB ACK를 대조했다.
10개 동시 producer와 2개 flush Worker의 경합, 별도 프로세스 재시작, SENT/UNKNOWN/HELD/POSTING 불변, 기존 ACK gap·restore REVIEW, VERIFIED 우선/동일 사건 차단을 검사했다.
같은 signal_id·다른 실제 주문 UUID는 기존 별도 REVIEW로 보존하고 임의 병합하거나 추가 송신하지 않는다. 같은 주문 UUID의 변경 신호는 충돌 REVIEW로 보존한다.
10개 서버의 서로 다른 로컬 DB 간 전역 중복 방지는 구현되어 있지 않다. 같은 Webhook이 여러 서버로 전달되는 운영 라우팅은 별도 대조가 필요하다.

## 기존 기능과 원본 pin 차이

main.py, safe_retry.py, outbox.py, verifier/evidence/bootstrap/history/ledger/pnl_calculator/reconciliation.py의 원문은 BASE_SHA와 동일하다.
delivery_store의 _wire/_attempt/begin_post/ack/claim_next/_notice_gate AST도 동일하다. OrderRequest의 변경은 tv_signal ID/시각 일치 validator에 한정하고 나머지 schema/실행 필드·validator AST 동일성을 확인했다.
기존 v4 canonical/signature, KRW v5 정상 원가·Net, cost151/Net8, 허위Net58, HISTORY 철회, POSTING ACK 경합·prune 보호 대표 회귀가 통과했다.
선물/Coin-M 6개 route 검사는 기존 주문 실행 경계와 REQUERY Intake를 유지하고 현물 신호 원가로 계산하지 않음을 확인했다. 선물 실제 손익·라이브 전달을 새로 증명한 것은 아니다.

원본 test_order_and_crypto_contracts_unchanged는 OrderRequest 전체 AST를 과거 c99be386에 고정하여, 승인된 알림 binding validator 추가 때문에 FAIL이다.
원본 test_main_order_and_financial_guards_unchanged는 delivery_store.py 전체 bytes를 과거6a84d84b에 고정하여 신규 unsigned 알림 snapshot3 admission 지원 때문에 FAIL이다.
두 검사의 원문·expected hash는 변경하지 않았다. 별도 test_new_notifications_only_ast_and_wire_guards_unchanged 및 실제 적대적 E2E가 변경 금지 영역 불변과 admission 안전성을 검증한다. pin FAIL을 기능 PASS로 재분류하지 않는다.

snapshot3/layout3는 unsigned 알림 메타데이터/표시 호환 버전이다. 신규 금융 receipt가 아니며 기존 snapshot2/layout1·2 및 SENT bytes를 재작성하지 않는다. 과거 pending v2도 원래 wire로 ACK한다.

## 운영 읽기 전용 진단

각 서버에서 기존 도구를 사용한다. 아래 명령은 이번 작업에서 실행하지 않았다.

```bash
cd /root/POA
git rev-parse HEAD 2>/dev/null || python -c 'import json; print(json.load(open(".poa-deployment.json"))["commit"])'
systemctl is-active poa
journalctl -u poa --since '30 minutes ago' --no-pager | grep -oE 'verified_alerts: [A-Za-z_0-9]+' | sort | uniq -c
/root/POA/.venv/bin/python scripts/notification_pipeline_status.py --db /root/POA/notifications.sqlite3 --events 40 --server-alias node-01
```

venv/알림 DB 경로는 기존 설치 설정에 맞춘다. .env 내용이나 주문 원문을 출력하지 않는다. script는 READ_ONLY 집계·해시 참조만 출력한다. Worker liveness와 실제 Discord 수신은 이 집계만으로 확정하지 않는다.
서버별 signal/order 지문·배포 SHA·TV_SIGNAL/ORDER_OBSERVATION 경로·사유코드·Outbox/ACK를 비교한다. 기존 NOTIFICATION_RECOVERY_UPDATE.md 절차대로 .env/DB/API 키를 보존하며 순차 업데이트하고, 재설치·DB 초기화·UNKNOWN/HELD 강제 재전송을 하지 않는다.
실제 Vultr→Discord 수신 시험은 운영자 승인과 격리된 테스트 Discord Webhook을 별도로 확보한 뒤 진행해야 한다.

RELEASE_MANIFEST_SHA256: 3ee5c14bfdaddcf0930c1e0951fad83418c96de0d822d337ecdbe4a24499068b
USER_DATA_PIN: 동일 값으로 동기화
계산 정확성: 지원 범위 PASS
Mock Discord 전달 계약: PASS
실제 Vultr→Discord 수신: UNVERIFIED
CODE_GATE=BLOCK / OPERATING_READY=NO
DEPLOYMENT_REQUIRED: 새 알림 수정 적용을 위한 운영자 순차 업데이트 필요. 이번 작업은 서버에 배포하지 않는다.
