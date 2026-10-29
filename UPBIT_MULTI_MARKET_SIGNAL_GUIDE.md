# Upbit KRW 전 종목 알림 / TradingView 공통 신호 템플릿

## 확인한 코드 원인

- Simple Host 선택, native 주문 정규화, notification Host, 인증 GET 응답 검사에 BTC/KRW 고정값이 있었습니다. 이제 **알림 전용** Host를 주문의 KRW 종목/전략별로 선택합니다. UUID/market/side/계정 키 참조/producer/lease를 대조하고 GET `/v1/order`만 사용합니다. 실제 존재하는 종목 여부는 거래소 조회 응답으로 확인하며, 미지원/미체결/부분취소/오류는 REVIEW/WAIT입니다.
- `tv_signal`이 없으면 기존 ORDER_OBSERVATION 경로를 사용합니다. 기존 주문용 price/amount/cost로 신호 가격을 합성하지 않습니다.
- `strategy_id`가 없으면 기존 order_name fallback을 사용합니다. LL과 Close_HL이 다른 이름이면 다른 전략으로 취급합니다. 이름만 보고 자동 병합하지 않습니다.
- ID-only ACK는 UUID만 영속 보존하며 수량/평균가격은 0/미확정입니다. 조회가 실패하면 체결 관측과 Outbox 생성까지 진행하지 않습니다. 매수 기록이 다른 전략에 저장됐거나 없으면 매도 원가는 UNAVAILABLE입니다.
- 실제 서버의 JSON, 로그, DB, Discord 스크린샷 원본은 제공받지 않았습니다. 서버의 설정 오류/Worker 중단/권한/중복 발송/여러 서버 혼합 중 실제 원인은 아직 확정하지 않았습니다.

금융 VERIFIED reader의 BTC/KRW 생성 제한, v4/v5 서명, HISTORY/원장, 주문·Safe Retry와 Outbox CAS/ACK는 변경하지 않았습니다. 신규 KRW 종목 reader에는 금융 adapter/HISTORY 발급 포트가 없습니다. 장기 보유 잔고/계정 평균매수가는 조회하지 않습니다.

## 공통 strategy_id가 필수

203개 전략은 **서로 다른 고정 strategy_id**를 갖고, 같은 전략의 진입·청산은 동일 ID를 사용해야 합니다. LL/Close_HL은 order_name/order_id로만 남깁니다. 종목+시간봉만으로 여러 전략을 합치거나, 서버가 전략 ID/신호 가격을 추측하지 않습니다.

최소 변경은 각 전략의 기존 주문 알림에 `alert_message` 메타 조각을 한 번 연결하고, 전체 전략에 같은 Webhook 템플릿을 쓰는 방식입니다. 전략별 JSON을 203개 따로 관리할 필요는 없지만, upstream이 주지 않는 전략 ID를 자동으로 알아낼 수는 없습니다.

## 전략 order fill 공통 템플릿

TradingView [전략 알림](https://www.tradingview.com/support/solutions/43000481368-strategy-alerts/)과 [alert_message 사용법](https://www.tradingview.com/pine-script-docs/faq/alerts/)에 따른 템플릿입니다. TradingView의 order fill은 브로커 에뮬레이터 이벤트이며 실제 거래소 체결 증거가 아닙니다.

Pine의 기존 주문 생성/청산 호출에 `alert_message` 인자만 추가합니다. 매수·매도 규칙이나 주문 수량을 변경하지 마십시오. 메타 조각은 아래와 같은 **따옴표가 포함된 두 JSON 필드**이며, 바깥 `{}`는 넣지 않습니다.

```text
"strategy_id":"S203-001","signal_id":"S203-001-20261030-1001"
```

같은 전략의 매수/매도는 strategy_id를 유지하고 signal_id만 각 사건마다 다르게 만듭니다. 실제 재전송에는 동일 ID를 사용합니다. `strategy.order.id`는 반복 사용되므로 단독 고유 신호 ID로 쓰지 않습니다. 여러 사건이 같은 초/봉에 발생할 수 있으므로 시각만으로 고유성을 보장하지 마십시오.

기존 Pine 코드에 재사용 가능한 문자열 함수 예시(신규 매매 규칙이 아님):

```pine
poaMeta(string strategyId, string eventId) =>
    '"strategy_id":"' + strategyId + '","signal_id":"' + eventId + '"'
// 기존 주문 호출에 alert_message=poaMeta(공통전략ID, 사건별고유ID)만 추가.
// 허용 문자 ID를 사용하고 기존 주문의 qty/price/진입·청산 조건은 유지.
```

아래 메시지는 **Order fills only** 알림에 공통으로 넣습니다. `{{strategy.order.alert_message}}`는 위 JSON 메타 조각으로 치환되어야 합니다. 치환 전 텍스트는 JSON이 아니지만, 전송될 때는 유효한 JSON이어야 합니다. 전체 객체나 단순 문자열을 메타 조각 대신 넣지 마십시오.

**상위 amount/percent/cost/price는 기존 실주문 설정을 그대로 유지하십시오. 아래 amount는 TV contracts를 주문 수량으로 쓰는 경우의 예시이며 기존 설정을 자동 대체하지 않습니다.**

```json
{
  "password": "YOUR_EXISTING_WEBHOOK_PASSWORD",
  "exchange": "UPBIT",
  "base": "{{syminfo.basecurrency}}",
  "quote": "{{syminfo.currency}}",
  "side": "{{strategy.order.action}}",
  "amount": "{{strategy.order.contracts}}",
  "order_name": "{{strategy.order.id}}",
  {{strategy.order.alert_message}},
  "tv_signal": {
    "kind": "strategy_order_fill",
    "price": "{{strategy.order.price}}",
    "quantity": "{{strategy.order.contracts}}",
    "action": "{{strategy.order.action}}",
    "order_id": "{{strategy.order.id}}",
    {{strategy.order.alert_message}}
  }
}
```

UPBIT은 KRW, BINANCE/BYBIT/OKX는 USDT 현물로 사용합니다. 거래소는 실제 POA 주문 계정의 거래소를 명시하십시오. 차트 거래소를 무조건 거래 계정이라고 추정하지 않습니다. `strategy_id`/`signal_id`를 기존 JSON에 이미 넣었다면 중복 키를 만들지 마십시오. 실제 키/Secret/Discord URL을 TradingView 메시지에 넣지 않습니다.

## alert() 방식: 완전한 명시값 매수/매도 예시

alert() 메시지의 price/quantity는 Pine에서 직접 문자열로 만듭니다. 아래는 합성 XRP 예시이며 실주문 지시가 아닙니다. strategy_id는 동일하고 order_name과 signal_id는 별개입니다.

```json
{
  "password": "YOUR_EXISTING_WEBHOOK_PASSWORD",
  "exchange": "UPBIT", "base": "XRP", "quote": "KRW",
  "side": "buy", "amount": 100, "order_name": "LL",
  "strategy_id": "S203-001", "signal_id": "S203-001-buy-1001",
  "tv_signal": {
    "kind": "alert", "price": "1000", "quantity": "100", "action": "buy",
    "order_id": "LL", "strategy_id": "S203-001", "signal_id": "S203-001-buy-1001"
  }
}
```

```json
{
  "password": "YOUR_EXISTING_WEBHOOK_PASSWORD",
  "exchange": "UPBIT", "base": "XRP", "quote": "KRW",
  "side": "sell", "amount": 100, "order_name": "Close_HL",
  "strategy_id": "S203-001", "signal_id": "S203-001-sell-1002",
  "tv_signal": {
    "kind": "alert", "price": "1100", "quantity": "100", "action": "sell",
    "order_id": "Close_HL", "strategy_id": "S203-001", "signal_id": "S203-001-sell-1002"
  }
}
```

이 예시의 표시는 평균 매수가1,000원, 매도가1,100원, 수량100 XRP, 신호 예상 손익+10,000원, ROI+10.00%입니다. 수수료는 미반영입니다.

매수 신호가 없을 때만 SELL의 tv_signal에 아래 두 필드를 보조로 넣을 수 있습니다.

```json
"entry_price": "1000",
"entry_source": "strategy.position_avg_price"
```

entry_price는 Pine의 실제 `strategy.position_avg_price`를 **청산 전 유효할 때** 문자열로 읽은 값이어야 합니다. 미문서화 `{{strategy.position_avg_price}}` placeholder를 가정하지 않습니다. flat 이후 0/NaN, 다른 전략의 평균, 거래소 전체 평균은 넣지 않습니다. 값이 없으면 두 필드를 모두 생략하고 계산 불가를 유지합니다. 이 값도 전략 기록 기반 예상치이며 VERIFIED 증거가 아닙니다.

## 화면·구버전 메시지

매수/매도에 전략, 신호 가격/수량, 가상 평균/잔량, 예상 손익·ROI, 가격 출처, 수수료 상태, KST 시각을 표시합니다. 체결이 미확정인 신호는 별도로 **주문 결과 확인 필요**를 유지합니다. 근거 부족은 안전한 사유 코드와 함께 표시합니다. tv_signal 없는 주문 관측은 신호라고 바꾸어 부르지 않습니다.

신규 알림은 표시 버전만 구분합니다(새 금융 receipt 아님). 기존 payload와 frozen wire는 기존 표시를 그대로 재현하여, 업데이트 자체가 이전 PENDING/SENT를 무효화하거나 원본 bytes를 바꾸지 않게 합니다. UNKNOWN/HELD/POSTING은 승인 없이 재전송하지 않습니다.

## 10대 서버 출처·읽기 전용 비교

선택 설정으로 각 서버의 `.env`에 비민감 별칭만 추가할 수 있습니다.

```text
POA_NOTIFICATION_SERVER_ALIAS=node-01
```

계정/포켓/금융 증빙을 의미하지 않습니다. 새 메시지에 별칭과 검증 가능한 배포 기록 SHA 축약을 표시합니다. 기록/실제 제품 해시가 맞지 않으면 SHA는 미확인입니다. 과거 SENT에는 소급 추가하지 않습니다.

```sh
cd /root/POA
/root/POA/.venv/bin/python scripts/notification_pipeline_status.py \
  --db /root/POA/notifications.sqlite3 --events 40 --server-alias node-01
```

출력은 배포 확인 상태, systemd 서비스 상태, **Worker 상태 미확인 여부**, TV_SIGNAL/ORDER_OBSERVATION/LEGACY 경로, 누락 코드, 상태 집계, 주문·신호의 비가역 SHA-256 지문입니다. API 키/계정/전략/주문 원문은 출력하지 않습니다. 같은 계정 키 참조가 포함된 지문은 서버 간 비교할 수 있습니다. 신호 ID 자체가 짧고 추측 가능하면 해시를 암호화처럼 간주하지 마십시오. 오래된 경로에 signal_id가 저장되지 않았다면 누락으로 표시하며 소급 생성하지 않습니다.

- 같은 order 지문 + 같은 signal 지문 + 다른 서버 별칭: 서버 중복 발행 가능성. 전달 이력과 시간 확인 필요.
- 다른 order 지문: 서로 다른 실제 주문일 수 있으므로 임의 병합하지 않습니다.
- 같은 서버에서 동일 사건 재시작: 로컬 Outbox/ACK/중복 방지 이력과 대조합니다.
- systemd active만으로 Worker가 건강하다고 단정하지 않습니다. 최근 `WORKER_TICK_OK`와 실패/차단 코드를 함께 확인합니다.

각 서버는 독립 DB이므로 서버 간 전역 Exactly-Once를 보장하지 않습니다. 같은 계정·전략 신호는 한 서버로 라우팅해야 합니다. 기존 여러 서버/같은 채널의 실제 중복 여부는 수집된 지문과 외부 메시지 대조 전까지 UNVERIFIED입니다.

## 기존 서버 업데이트

[데이터 보존 업데이트 절차](NOTIFICATION_RECOVERY_UPDATE.md)를 사용합니다. 재설치·DB 초기화·실거래 테스트를 하지 않습니다. 한 대에서 유입 중지→정상 정지→일관된 .env/DB/Sidecar 백업→고정 SHA/manifest 검증→제품 파일만 교체→운영자 승인 후 시작·Worker 진단→나머지 서버 순차 진행합니다.

업데이트 명령은 `.poa-deployment.json`에 비민감 commit/manifest 참조도 기록합니다. 이 기록은 제품 해시 대조용이며 계정 신원이나 금융 증거가 아닙니다. 기록이 없는 기존 설치는 배포 SHA를 추측하지 않습니다. TradingView 알림은 스크립트·입력의 snapshot이므로 Pine/JSON을 수정한 뒤 기존 알림을 갱신 또는 재생성해야 합니다. 서버 코드 업데이트만으로 과거 JSON에 tv_signal이 추가되지 않습니다.

같은 signal_id라도 다른 실제 주문 UUID가 들어오면 원래 주문에 합치지 않고 별도 Intake REVIEW(`TV_SIGNAL_ORDER_CONFLICT`)로 영속 보존합니다. 추가 원가/Root/자동 POST를 만들지 않으며, 서로 다른 주문 지문과 같은 신호 지문을 운영자가 확인할 수 있습니다. 같은 주문 UUID·같은 신호의 재전송은 기존 Intake로 수렴합니다.

현재 종목 확대는 기본 Simple ESTIMATED Host에 적용됩니다. 기존 외부 등록/엄격한 금융 scope를 임의 확대하지 않습니다. 주문 단위 관측의 체결 목록 한도(40개), 부분취소 REVIEW, API 권한/IP 제한, 미확정 전송 보호는 유지합니다.

## 검증 요약

관련 고유 209 PASS / 최종 FAIL 0입니다. 원본 종목 혼입 4건은 BUY 복구에 따른 기대값만 보완하고 잘못된 SELL의 Root/Delivery/POST 0을 강화해 4 PASS로 확인했습니다. JWT 검사도 BUY/SELL을 명시해 기존 nonce·서명·Net/ROI 조건을 보존했습니다. 기존 UNKNOWN/HELD 2건의 계약 예외와 원본 expected hash는 변경하지 않았습니다. 실제 API·거래·Discord·운영 서버 시험은 하지 않았습니다.

승인된 금융 provider가 다른 종목/거래소/계정 범위인 경우에는 그 provider에 다른 주문을 강제로 연결하지 않고 알림 전용 관측 경로를 사용합니다. 일치하는 기존 VERIFIED 경로의 우선순위와 검증 조건은 유지합니다.
