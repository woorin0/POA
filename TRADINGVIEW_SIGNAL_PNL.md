# TradingView 신호 기준 예상 손익 설정

기존 설치의 도메인·Webhook 비밀번호·거래소 키·Discord Webhook 설정을 그대로 사용합니다. 기존 현물 `/order` Webhook JSON에 `strategy_id`와 `tv_signal`만 추가합니다. 기존 알림/Outbox 설정이 활성화돼 있어야 합니다. `tv_signal`이 없는 메시지는 기존 경로를 유지합니다.

## 주문 실행과 분리

상위 `amount`/`price`는 기존 주문 실행용입니다. `tv_signal.price`/`quantity`는 알림 계산용이며 주문 크기나 주문가격을 바꾸지 않습니다. 자동 복사·대체하지 않습니다. 원래 주문 처리 후 `main.log`에 주문 ID가 전달될 때 별도 Intake에 커밋됩니다. 주문 ID 자체가 없거나 주문 처리/프로세스가 그 전에 실패한 입력은 신호 포지션에 추가하지 않습니다. 주문 실패 Embed와 기존 Safe Retry는 변경하지 않았습니다.

API 키 식별 해시·거래소·전략·종목마다 별도 가상 포지션을 사용합니다. 전체 계정 잔고·거래소 평균매수가·장기 보유분·기존 실제 체결 관측 원가는 조회하거나 합치지 않습니다. API 키나 전략 ID를 바꾸면 별도 포지션입니다. 신호 수신 순서로 가중평균을 계산하며, 부분매도는 매칭된 수량의 원금만 차감합니다. 보유 수량 초과분은 따로 표시하고 계산하지 않습니다.

이 경로는 실제 거래소 체결을 확인하지 않습니다. 금융 영수증을 생성하지 않고 신호를 `confirmed=True`로 변경하지 않습니다. 화면은 항상 **TradingView 신호 기준 예상 손익**, 수수료 미반영으로 표시합니다. 기존 금융 VERIFIED·서명·취소/정정·전송 승인은 그대로입니다.

## 전략 order fill 알림

TradingView의 [공식 전략 알림 문서](https://www.tradingview.com/support/solutions/43000481368-strategy-alerts/)에 정의된 price/contracts/action/id를 다음 위치에 넣습니다. order fill은 TradingView 시뮬레이션 체결이며 거래소 체결 증거가 아닙니다.

```json
{
  "password": "YOUR_EXISTING_WEBHOOK_PASSWORD",
  "exchange": "UPBIT",
  "base": "BTC",
  "quote": "KRW",
  "side": "{{strategy.order.action}}",
  "amount": 0.001,
  "strategy_id": "btc-spot-A",
  "signal_id": "{{strategy.order.alert_message}}",
  "tv_signal": {
    "kind": "strategy_order_fill",
    "price": "{{strategy.order.price}}",
    "quantity": "{{strategy.order.contracts}}",
    "action": "{{strategy.order.action}}",
    "order_id": "{{strategy.order.id}}",
    "signal_id": "{{strategy.order.alert_message}}",
    "strategy_id": "btc-spot-A"
  }
}
```

`amount` 예시는 기존 실행 수량일 뿐입니다. 자신의 기존 주문 설정을 유지하십시오. `signal_id`는 Pine에서 **매 이벤트마다 유일하고 재전송 시 동일한 ID**를 `alert_message`로 지정해야 합니다. 예: 전략 고유 접두사+주문 일련번호. `strategy.order.id`는 반복 사용되므로 단독 중복 키로 사용하지 않습니다. 빈 값/미치환 `{{...}}`, NaN/Infinity, 0 이하 숫자, 과도한 길이/제어문자를 거부합니다. 가격·수량은 문자열로 보내십시오. action/strategy는 상위 주문과 같아야 하며 선물 신호는 거부합니다.

중복 키는 계정 키 식별자·거래소·전략·종목·신호 ID입니다. 동일 사실은 기존 Intake로 수렴하고 다른 가격/수량으로 재사용하면 거부합니다. 이는 **알림 중복 방지**이며 주문 실행의 중복 처리 규칙을 대체하지 않습니다. Upbit Safe Retry의 상위 `signal_id`도 기존처럼 필요합니다.

## alert() 명시적 메시지

`alert()`에서는 strategy order placeholder를 추정하지 않습니다. Pine에서 실제 문자열 값을 조립해 아래와 같이 전달합니다. 상위 주문 필드는 기존 설정입니다.

```json
{
  "password": "YOUR_EXISTING_WEBHOOK_PASSWORD",
  "exchange": "UPBIT",
  "base": "XRP",
  "quote": "KRW",
  "side": "sell",
  "amount": 100,
  "strategy_id": "xrp-spot-A",
  "signal_id": "xrp-A-exit-002",
  "tv_signal": {
    "kind": "alert",
    "price": "1100",
    "quantity": "100",
    "action": "sell",
    "order_id": "exit",
    "signal_id": "xrp-A-exit-002",
    "strategy_id": "xrp-spot-A"
  }
}
```

사전에 같은 계정/전략/XRP의 buy 신호(price=1000, quantity=100)를 저장했다면 예상 손익은 +10,000원, ROI +10.00%입니다. 실제 실행 수량과 신호 수량이 다를 수 있으며, 신호 수량은 현물 자산 단위로 제공해야 합니다.

매수 기록이 전혀 없을 때만 매도 `tv_signal`에 `entry_price`와 `entry_source: "strategy.position_avg_price"`를 명시해 보조할 수 있습니다. 이는 비밀번호로 인증된 Webhook의 명시적 전략 기록일 뿐 거래소 검증 근거가 아닙니다. 출처를 화면에 별도로 표시합니다. 두 필드 중 하나만 있으면 거부합니다. 기존 매수 기록이 있으면 이 보조값으로 덮어쓰지 않습니다. 근거가 없으면 계산 불가입니다.

## 모바일 Discord 예시

```text
TradingView 신호 기준 예상 손익
전략: btc-spot-A
거래소: UPBIT / 종목: BTC/KRW
매수 신호 평균가: 100,000,000원
매도 신호가격: 110,000,000원
매칭 수량: 1 BTC
신호 기준 예상 손익: +10,000,000원
예상 ROI: +10.00%
가격 출처: TradingView 신호 기록
수수료: 미반영
실제 체결·정산손익 아님
```

```text
TradingView 신호 기준 예상 손익
전략: xrp-spot-A
거래소: UPBIT / 종목: XRP/KRW
매수 신호 평균가: 1,000원
매도 신호가격: 1,100원
매칭 수량: 100 XRP
신호 기준 예상 손익: +10,000원
예상 ROI: +10.00%
가격 출처: TradingView 신호 기록
수수료: 미반영
실제 체결·정산손익 아님
```

KRW는 원, USDT는 USDT입니다. 일반 신호 계산에는 BTC-only 제한이 없습니다. 기존 Upbit 인증 체결조회 Host의 BTC/KRW 지원 범위를 확장한 것은 아닙니다.

## 영속성과 한계

- 기존 Sidecar WAL/FULL 커밋과 원본 요청 digest, v4 submission lineage, restore gate, frozen wire/lease/ACK를 재사용합니다. 전송 미확정 UNKNOWN/HELD는 자동 재전송하지 않습니다. 기존 SENT는 수정하지 않습니다.
- 새로운 신호 중복 참조 테이블만 Sidecar에 추가하며 기존 행의 의미·원본 요청을 변경하지 않습니다. Sidecar/v4 백업·복원 대조 절차는 계속 필요합니다.
- 동일 사건의 기존 VERIFIED/ESTIMATED 발행 차단 규칙을 유지합니다. 이미 SENT된 사건을 신호 알림으로 재전송하지 않습니다.
- 수신 누락·시간 역전·다른 전략 ID·키 교체로 가상 포지션이 실제 전략과 달라질 수 있습니다. 자동 보유량 대조·과거 신호 복원·신호 순서 재배치는 하지 않습니다. 스트림 4096행을 넘으면 계산 불가로 중단합니다.
- 실제 서버/거래소/Discord 시험은 수행하지 않았습니다. 운영 준비 완료나 금융 VERIFIED 승인으로 해석하지 마십시오.

## 매수 알림 복구

승인된 매수 신호도 Outbox 알림 1건을 생성합니다. 가격·신호수량·전략 평균매수가·가상 보유량·KST 시각을 표시하며, 실제 체결 증거가 없으면 “주문 체결 확인 중”으로 표시합니다. 매수 알림에는 손익/ROI가 없습니다. 매도 가중평균·부분매도 계산은 그대로 유지합니다. 서버별 진단과 데이터 보존 업데이트는 [NOTIFICATION_RECOVERY_UPDATE.md](NOTIFICATION_RECOVERY_UPDATE.md)를 참조하십시오.

## KRW 종목·공통 전략 연결

최신 알림용 Upbit Simple Host는 BTC/XRP/LINK 등 KRW 현물 주문 종목으로 선택합니다. 기존 VERIFIED 금융 scope는 확대하지 않습니다. LL/Close_HL에 공통 strategy_id를 전달하는 공통 JSON은 [UPBIT_MULTI_MARKET_SIGNAL_GUIDE.md](UPBIT_MULTI_MARKET_SIGNAL_GUIDE.md)를 사용하십시오. `tv_signal` 없는 기존 JSON을 서버가 임의 변환하지 않습니다.

동일 신호 ID에 서로 다른 실제 주문 UUID가 관측되면 별도 REVIEW에 보존하고 자동 합병/전송하지 않습니다. `DURABLE_REVIEW`는 저장 성공이며 알림 전달 성공이 아닙니다.
