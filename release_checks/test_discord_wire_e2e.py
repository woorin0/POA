"""Offline Discord HTTP-boundary E2E against genuine POA read, Outbox and sender.

Only external boundaries are synthesized:
- venue-native GET payloads reuse the PR4 source-contract fixtures
- real requests.post/Session/PreparedRequest and DiscordSender._send_wire run
- HTTPAdapter.send is replaced by an in-memory fake Discord HTTP endpoint
- release_checks.isolation denies sockets, DNS and non-temp SQLite.

No actual exchange API, trading action, webhook, operating server or live DB.
The HTTP-boundary fake is not real Discord receipt.
"""
from __future__ import annotations

import copy
import json
from decimal import Decimal as D, localcontext
from urllib.parse import urlsplit, parse_qs

import pytest
import requests
from requests.api import post as genuine_requests_post
from requests.adapters import HTTPAdapter

import fill_confirmation_contracts as F
from exchange.verified_pnl import dispatcher as DP
from exchange.verified_pnl import order_fill_confirmation as C
from exchange.verified_pnl import outbox
from exchange.verified_pnl.order_fill_read import CONTRACT

MARKETS = F.MARKETS


def decimal_field(description, label):
    prefix = label + ": "
    matches = [line[len(prefix):].split()[0].removesuffix("원")
               for line in description.splitlines() if line.startswith(prefix)]
    assert len(matches) == 1, (label, description)
    return D(matches[0])


class FakeDiscordEndpoint:
    """In-process HTTP receiver accepting real Requests PreparedRequest objects."""
    def __init__(self, confirmation_responses=()):
        self.calls = []
        self.responses = list(confirmation_responses)
        self.messages = []
        self.success_id = 10000000

    def send(self, adapter, prepared, **kwargs):
        # No socket is used; release_checks.isolation separately rejects every
        # socket connection and DNS lookup if anything escapes this adapter.
        parsed = urlsplit(prepared.url)
        assert prepared.method == "POST"
        assert parsed.scheme == "https"
        assert parsed.netloc == "discord.com"
        assert parsed.path == "/api/webhooks/000/synthetic"
        assert parse_qs(parsed.query) == {"wait": ["true"]}
        assert prepared.headers.get("Content-Type") == "application/json"
        assert kwargs.get("timeout") == 5
        assert kwargs.get("allow_redirects") is None  # handled by Session.send
        assert isinstance(prepared.body, bytes)
        payload = json.loads(prepared.body.decode("utf-8"))
        assert payload["allowed_mentions"] == {"parse": []}
        assert len(payload["embeds"]) == 1
        is_confirmation = "확인 방식: 거래소 인증 체결 조회" in payload["embeds"][0].get("description", "")
        self.calls.append({"confirmation": is_confirmation, "wire": prepared.body, "payload": payload})
        outcome = self.responses.pop(0) if is_confirmation and self.responses else 200
        if outcome == "timeout":
            raise requests.exceptions.Timeout("synthetic transport timeout")
        response = requests.Response()
        response.status_code = int(outcome)
        response.url = prepared.url
        response.request = prepared
        response.encoding = "utf-8"
        response.headers["Content-Type"] = "application/json"
        if outcome == 429:
            response.headers["Retry-After"] = "2"
        if outcome == 200:
            self.success_id += 1
            message = {"id": str(self.success_id)}
            self.messages.append({"id": message["id"], "payload": payload})
            response._content = json.dumps(message).encode("utf-8")
        else:
            response._content = json.dumps({"error": "synthetic status", "retry_after": 2}).encode("utf-8")
        return response

    @property
    def confirmations(self):
        return [x for x in self.calls if x["confirmation"]]


def connected_fake_http(r, monkeypatch, responses=()):
    """Restore genuine requests.post overridden by the inherited PR4 fixture."""
    endpoint = FakeDiscordEndpoint(responses)
    monkeypatch.setattr(requests, "post", genuine_requests_post)
    assert outbox.requests.post is genuine_requests_post
    monkeypatch.setattr(HTTPAdapter, "send", lambda adapter, prepared, **kwargs: endpoint.send(adapter, prepared, **kwargs))
    return endpoint


def ready_and_send(r):
    F.feed(r)  # original registered POA producer -> Intake
    F.tick(r)  # actual verification_tick -> ReadHost -> Outbox
    DP.flush_outbox(r.st)  # actual DiscordSender -> requests.post
    F.tick(r)
    DP.flush_outbox(r.st)


def confirmation_status(r):
    with r.store()._connect_read() as db:
        rows = db.execute(
            "SELECT d.status,d.message_id,d.wire,d.attempts "
            "FROM vpnl_deliveries d JOIN vpnl_revisions v "
            "ON v.root_id=d.root_id AND v.revision=d.revision "
            "WHERE json_extract(v.payload,'$.notification_type')=?",
            (CONTRACT,),
        ).fetchall()
    assert len(rows) == 1, rows
    return rows[0]


def verify_display(embedded, exchange, future, side, count, *, short=False, close=False):
    """Independent fixed native fixture arithmetic; not sourced from POA proof."""
    title = embedded["title"]
    description = embedded["description"]
    with localcontext() as ctx:
        ctx.prec = 512
        each_qty = D("0.2") if exchange == "OKX" and future else D("1")
        qty = D(count) * each_qty
        total = sum((D(100 + i*10) * each_qty for i in range(count)), D(0))
        avg = total / qty
    assert exchange in title
    assert ("SOL/KRW" if exchange == "UPBIT" else "SOL/USDT" + (":USDT" if future else "")) in title
    assert ("매수" if side == "buy" else "매도") in title
    # Explicit market type is required, not inferred from instrument notation.
    assert ("선물" if future else "현물") in title + description, "MARKET_TYPE_NOT_EXPLICIT"
    if future:
        assert (("숏" if short else "롱") + (" 청산" if close else " 진입")) in title
    label = "계약 명목가치" if future else ("실제 총 매수액" if side == "buy" else "실제 총 매도액")
    assert decimal_field(description, "실제 평균 체결가") == avg
    assert decimal_field(description, "실제 체결수량") == qty
    assert decimal_field(description, label) == total
    assert ("원" if exchange == "UPBIT" else " USDT") in description
    assert "상태: 완료 주문의 전체 체결 확인" in description
    assert "KST" in description
    assert "수수료 제외" in description
    assert "금융 손익 검증 영수증이 아닌 조회 기반 확인" in description


@pytest.mark.parametrize("exchange,future", MARKETS)
@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("count", [1, 2])
def test_real_sender_wire_all_markets(tmp_path, monkeypatch, exchange, future, side, count):
    """28: Actual POA producer -> Intake -> GET proof -> Outbox -> sender -> HTTP boundary."""
    r = F.setup(tmp_path, monkeypatch, exchange, future, side, count=count,
                close=future and side=="sell")
    http = connected_fake_http(r, monkeypatch)
    ready_and_send(r)
    confirmations = http.confirmations
    assert len(confirmations) == 1, confirmations
    payload = confirmations[0]["payload"]
    embed = payload["embeds"][0]
    verify_display(embed, exchange, future, side, count,
                   close=future and side=="sell")
    status, message_id, wire, attempts = confirmation_status(r)
    assert status == "SENT" and message_id is not None and message_id.isdigit()
    assert wire == confirmations[0]["wire"]
    assert attempts == 1
    assert any(x["id"] == message_id for x in http.messages)
    prior = len(http.calls)
    F.tick(r); DP.flush_outbox(r.st); F.feed(r); F.tick(r); DP.flush_outbox(r.st)
    assert len(http.calls) == prior, "replay caused a duplicate HTTP request"
    assert r.posts == [], "fixture's short-circuit Discord mock must not be used"


@pytest.mark.parametrize("exchange", ["BYBIT", "BINANCE", "OKX"])
@pytest.mark.parametrize("short,close", [
    (False,False), (False,True), (True,False), (True,True),
], ids=["long-open", "long-close", "short-open", "short-close"])
def test_real_sender_futures_four_directions(tmp_path, monkeypatch, exchange, short, close):
    """12: Decode actual frozen HTTP JSON, not a substituted POA renderer."""
    side = "buy" if short == close else "sell"
    r = F.setup(tmp_path, monkeypatch, exchange, True, side, count=2, short=short, close=close)
    http = connected_fake_http(r, monkeypatch)
    ready_and_send(r)
    assert len(http.confirmations)==1
    verify_display(http.confirmations[0]["payload"]["embeds"][0],exchange,True,side,2,short=short,close=close)
    assert confirmation_status(r)[0]=="SENT"


@pytest.mark.parametrize("exchange,future,side,delay", [
    ("UPBIT",False,"buy",10),
    ("BYBIT",False,"sell",60),
    ("BINANCE",True,"buy",300),
    ("OKX",True,"sell",1800),
])
def test_no_premature_wire_before_delayed_fill(tmp_path,monkeypatch,exchange,future,side,delay):
    """4: Delayed limit fills cannot send premature confirmation wire."""
    r=F.setup(tmp_path,monkeypatch,exchange,future,side,count=2,
              close=future and side=="sell")
    http=connected_fake_http(r,monkeypatch)
    final=copy.deepcopy(r.state)
    F._set_native_pending(r)
    clock=[F.time.time()]
    monkeypatch.setattr(C.time,"time",lambda:clock[0])
    F.feed(r); F.tick(r);DP.flush_outbox(r.st)
    assert http.confirmations==[]
    with r.store()._connect_read() as db:
        assert db.execute("SELECT state,error FROM vpnl_fill_requests").fetchone()==("WAIT_READ","FILL_ORDER_PENDING")
    clock[0]+=delay
    r.state.clear();r.state.update(final)
    F.tick(r);DP.flush_outbox(r.st);F.tick(r);DP.flush_outbox(r.st)
    assert len(http.confirmations)==1
    verify_display(http.confirmations[0]["payload"]["embeds"][0],exchange,future,side,2,
                   close=future and side=="sell")
    assert confirmation_status(r)[0]=="SENT"


@pytest.mark.parametrize("exchange,future",MARKETS)
def test_partial_is_never_early_confirmed(tmp_path,monkeypatch,exchange,future):
    """7: partial -> fully filled, single final successful HTTP confirmation."""
    r=F.setup(tmp_path,monkeypatch,exchange,future,side="buy",count=2)
    http=connected_fake_http(r,monkeypatch)
    complete=copy.deepcopy(r.state)
    F._set_native_pending(r,partial=True)
    clock=[F.time.time()]
    monkeypatch.setattr(C.time,"time",lambda:clock[0])
    F.feed(r);F.tick(r);DP.flush_outbox(r.st)
    assert http.confirmations==[]
    clock[0]+=60
    r.state.clear();r.state.update(complete)
    F.tick(r);DP.flush_outbox(r.st)
    assert len(http.confirmations)==1
    assert confirmation_status(r)[0]=="SENT"


@pytest.mark.parametrize("outcome,expected",[(429,"PENDING"),(500,"UNKNOWN"),("timeout","UNKNOWN"),(401,"FAILED")])
def test_real_sender_http_status_policy(tmp_path,monkeypatch,outcome,expected,capsys):
    """4: genuine DiscordSender handles remote 429/500/timeout/401 and records DB state."""
    r=F.setup(tmp_path,monkeypatch,"UPBIT",False,side="sell")
    r.st.DISCORD_RETRY_ENABLED=True
    http=connected_fake_http(r,monkeypatch,responses=[outcome])
    ready_and_send(r)
    assert len(http.confirmations)==1
    state, message_id, wire, attempts=confirmation_status(r)
    assert state==expected,(outcome,state)
    assert message_id is None
    assert attempts==1
    prior=len(http.calls)
    F.tick(r);DP.flush_outbox(r.st)
    assert len(http.calls)==prior, "unexpected auto-repeat after ambiguous/failed response"
    captured=capsys.readouterr()
    assert "synthetic-" + "s"*64 not in captured.out+captured.err
    assert "https://discord.com/api/webhooks/000/synthetic" not in captured.out+captured.err


def test_retry_after_429_then_sent_once(tmp_path,monkeypatch):
    """429 retries once without duplicate after successful ack."""
    r=F.setup(tmp_path,monkeypatch,"BYBIT",False,side="buy")
    r.st.DISCORD_RETRY_ENABLED=True
    http=connected_fake_http(r,monkeypatch,responses=[429,200])
    ready_and_send(r)
    assert len(http.confirmations)==1
    assert confirmation_status(r)[0]=="PENDING"
    # Advance only the in-memory/temp SQLite due gate; never alter live DB.
    with r.store()._connect() as db:
        db.execute("UPDATE vpnl_deliveries SET due=0 WHERE status='PENDING'")
    DP.flush_outbox(r.st)
    assert len(http.confirmations)==2
    assert confirmation_status(r)[0]=="SENT"
    prior=len(http.calls)
    F.tick(r); DP.flush_outbox(r.st)
    assert len(http.calls)==prior


def test_worker_recovery_no_second_discord_post(tmp_path,monkeypatch):
    """Fresh logical worker session recovery after persisted SENT."""
    r=F.setup(tmp_path,monkeypatch,"OKX",False,side="buy")
    http=connected_fake_http(r,monkeypatch)
    ready_and_send(r)
    assert confirmation_status(r)[0]=="SENT"
    before=len(http.calls)
    C._HOSTS.pop(r.store().db_path,None)
    C.prepare(r.st)
    r.store().recover()
    F.tick(r);DP.flush_outbox(r.st)
    assert confirmation_status(r)[0]=="SENT"
    assert len(http.calls)==before
