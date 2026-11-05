# 거래소 조회 기반 체결금액 확인

계약: `POA_EXCHANGE_ORDER_FILL_CONFIRMATION_V1`.
금융 EXECUTION/ACCOUNTING/FINANCIAL 영수증과 별도인 알림 전용 계약이다.
기본값은 OFF이며 이 문서는 서버 설정이나 서비스를 변경하지 않는다.

## 범위와 활성화

운영자 승인과 서버별 별도 검증 후 기존 설정 전달 경로에
`POA_EXCHANGE_FILL_CONFIRMATION_ENABLED=true`를 명시한다.
기존 `VERIFIED_DISCORD_ALERTS`, `VERIFIED_FILL_PRICE`,
`DISCORD_OUTBOX_ENABLED`도 유효해야 하며 UPBIT에는 기존
`UPBIT_VERIFIED_ALERTS`가 필요하다. 이 기능은 다른 플래그를 켜지 않는다.
해당 거래소의 기존 POA Settings 키/secret(OKX passphrase 포함)를 재사용한다.
키가 없으면 해당 거래소만 차단한다. 조회 전용 클라이언트가 키의 실제
거래 권한을 제거하는 것은 아니다. 키/IP/조회 권한은 별도 운영 확인 대상이다.

| 거래소 | 지원 | 조회 |
|---|---|---|
| UPBIT | KRW 현물 | UUID 기반 GET /v1/order |
| BYBIT | USDT 현물, USDT 선형 무기한 | 주문 history/realtime + execution/list |
| BINANCE | USDT 현물, USDT-M 선형 | 주문 + myTrades/userTrades |
| OKX | USDT 현물, USDT 선형 SWAP | 주문 + fills-history + instruments |

BTC/ETH/SOL 및 같은 단위 계약의 종목을 처리한다. Coin-M/inverse,
다른 정산통화, 만기형 상품은 지원으로 간주하지 않는다.
OKX SWAP은 원래 POA 주문 메타데이터의 contract_size와 조회한 ctVal을 대조한다.
선물 계약수량을 기초자산 수량으로 한 번만 환산하며 금액은 계약 명목가치다.
롱/숏 진입·청산은 조회 메타데이터로 확인할 수 있을 때만 표시한다.

## 원천·저장·전송

실제 main.log의 등록된 producer와 저장된 주문 ID만 신규 수집한다.
원본 Intake/request_digest/sidecar ID는 바꾸지 않는다. 원본 Intake와 같은
트랜잭션에 별도 불변 수집 의도를 기록하여 알림 DB 쓰기 실패 후 복구한다.
기능 활성화 이전 자료를 스캔하거나 WAIT_PROVIDER를 강제 승격하지 않는다.
선물 금융 provider가 없어도 별도 비금융 조회 job/submission을 사용할 수 있다.

인증 GET의 주문·종목·방향·credential reference·최종 상태, 전체 체결,
수량·거래대금·평균가·단위·페이지 종료를 검증한다. 수수료는 통화별로 분리한다.
임의 오차 허용 없이 Decimal로 대조하므로 반올림된 원천 평균가가 정확한
체결합계와 불일치하면 REVIEW다. 제한을 초과한 페이지도 확정하지 않는다.
진행 중 주문과 부분취소는 이 계약의 최종 완료 주문으로 승인하지 않는다.

원천 digest, 수집 intent, 읽기 lease, 원본 Intake 및 DB cutover lineage를
보존하고 enqueue/claim/기존 pre-POST 경계에서 다시 검증한다.
이 증거는 거래소 서명 영수증이 아니며 금융 손익 검증 권한을 부여하지 않는다.
로컬 DB 관리자나 악성 Python 코드에 대한 보안 샌드박스라고 주장하지 않는다.

기존 접수 SENT 뒤 확인 1건, 접수가 없으면 최초 확인 1건만 허용한다.
PENDING/CLAIMED는 대기하고 UNKNOWN/HELD/POSTING 및 모호한 상태는 자동
발송하지 않는다. 동일 실제 주문의 전략 충돌은 REVIEW다.
동일 주문 ID가 종목별로 재사용되는 거래소를 고려해 영속 identity에 종목도 포함한다.
Discord 응답이 불명확하면 기존 UNKNOWN 보호를 유지한다. 외부 정확히 한 번
전달을 보장하지 않는다. 과거 SENT/Frozen Wire를 수정하지 않는다.

## 복구와 검증 한계

새 테이블/인덱스/트리거만 추가하며 기존 schema version은 변경하지 않는다.
합성 DB backup/restore, 구버전 열기, 별도 프로세스 재시작 및 경합을 검증한다.
구버전은 신규 유형을 격리하므로 롤백 전에 신규 큐 처리 정책을 운영자가 확인해야 한다.
새 운영 기록이 있는 DB를 과거 백업으로 자동 되돌리지 않는다.

독립 검증: `python release_checks/run_fill_confirmation.py -q`.
기존 실제 Ed25519/금액/설치 안전 게이트: `python release_checks/run.py -q`.
격리 테스트는 실제 검증·Admission·상태전이·Frozen Wire를 사용하고
거래소 HTTPS 및 Discord HTTP IO만 Fake로 대체한다.
실계정 권한, 실 API 성공, Vultr 복원, 실제 Discord 수신은 별도 UNVERIFIED다.

## 원천 문서

- [UPBIT 개별 주문](https://docs.upbit.com/kr/reference/get-order)
- [BYBIT 주문](https://bybit-exchange.github.io/docs/v5/order/order-list), [체결](https://bybit-exchange.github.io/docs/v5/order/execution)
- [BINANCE 현물](https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints), [USDT-M](https://developers.binance.com/docs/derivatives/usds-margined-futures/trade/rest-api/Account-Trade-List)
- [OKX V5](https://www.okx.com/docs-v5/en/)

OPERATING_READY=NO. 실주문·실거래소 API·실제 Discord·서버 배포·main 병합은 수행하지 않는다.
