import hashlib
import json
import math
import random
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from typing import Optional, Dict, Any, List

import requests
try:
    from dhooks import Embed
    if hasattr(Embed, "to_dict") and not hasattr(Embed, "data"):
        try:
            Embed.data = property(lambda self: self.to_dict())
        except Exception:
            pass
except ImportError:
    class Embed:
        def __init__(self, **kw):
            self.data = dict(kw)
            self.data.setdefault("fields", [])

        def add_field(self, **kw):
            self.data.setdefault("fields", []).append(kw)

from loguru import logger

from .models import (
    NotificationPayload,
    OutboxStatus,
    ExitReason,
    PnlResult,
    FillStatus,
    VerificationStatus,
)

KST = timezone(timedelta(hours=9))


def format_kst(utc_timestamp: Optional[float]) -> str:
    if utc_timestamp is None:
        return "미확인"
    try:
        dt = datetime.fromtimestamp(float(utc_timestamp), tz=timezone.utc).astimezone(KST)
        return dt.strftime("%y-%m-%d %H:%M:%S")
    except Exception:
        return str(utc_timestamp)


def format_decimal(val: Optional[Any], decimals: int = 4) -> str:
    if val is None or val == "":
        return "-"
    try:
        d = Decimal(str(val))
        if not d.is_finite():
            return "-"
        formatted = f"{d:,f}"
        return formatted.rstrip("0").rstrip(".") if "." in formatted else formatted
    except Exception:
        return str(val)


def ensure_quarantine_schema(con):
    from .ledger import ensure_quarantine_schema as ensure
    ensure(con)


_INITIALIZED_DBS = set()


from .delivery_store import NotificationOutboxStore


def build_verified_embed(payload: NotificationPayload) -> Embed:
    """Builds the POA v2 unified mobile-friendly Discord Embed for verified order execution."""
    try:
        from exchange.utility.discord_embed import build_mobile_verified_embed
    except ModuleNotFoundError:
        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location(
            "exchange.utility.discord_embed",
            Path(__file__).resolve().parents[1] / "utility" / "discord_embed.py",
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules["exchange.utility.discord_embed"] = mod
        spec.loader.exec_module(mod)
        build_mobile_verified_embed = mod.build_mobile_verified_embed

    embed = build_mobile_verified_embed(payload)
    return embed


class DiscordSender:
    """Dispatches notifications to Discord with safe delivery-unknown holding on 5xx/timeouts (P2-5)."""

    def __init__(self, webhook_url: Optional[str] = None, retry_on_5xx: bool = False):
        self.webhook_url = (webhook_url or "").replace("discordapp", "discord")
        self.retry_on_5xx = retry_on_5xx
        self.message_id = None

    def send_payload(self, payload: NotificationPayload) -> tuple[bool, Optional[str], Optional[float]]:
        """Sends payload to Discord.
        Returns: (success: bool, error_message: Optional[str], retry_delay: Optional[float])
        """
        url = urlparse(self.webhook_url)
        if url.scheme != "https" or url.hostname not in {"discord.com", "canary.discord.com", "ptb.discord.com"} or not url.path.startswith("/api/webhooks/"):
            return False, "유효한 DISCORD_WEBHOOK_URL이 설정되지 않았습니다", None

        embed = build_verified_embed(payload)
        return self.send_embed(embed)

    def send_frozen(self, wire: bytes):
        return self._send_wire(wire)

    def send_embed(self, embed: Embed) -> tuple[bool, Optional[str], Optional[float]]:
        if not self.webhook_url:
            return False, "No webhook URL configured", None

        from .evidence import canonical
        data = {"embeds": [embed.data], "allowed_mentions": {"parse": []}}
        return self._send_wire(canonical(data).encode())

    def _send_wire(self, wire):
        url=urlparse(self.webhook_url)
        if url.scheme!="https" or url.hostname not in {"discord.com","canary.discord.com","ptb.discord.com"} or not url.path.startswith("/api/webhooks/"):
            return False,"send_failed",None
        try:
            resp = requests.post(self.webhook_url, params={"wait":"true"}, data=wire, headers={"Content-Type":"application/json"}, timeout=5, allow_redirects=False)
            if resp.status_code in (200, 204):
                try:
                    body = resp.json()
                    self.message_id = str(body["id"]) if isinstance(body,dict) and str(body.get("id", "")).isdigit() and len(str(body["id"]))<=32 else None
                except Exception:
                    pass
                return True, None, None

            if resp.status_code == 429:
                retry_after = 5.0
                try:
                    header = resp.headers.get("Retry-After")
                    retry_after = float(header) if isinstance(header,str) else float(resp.json().get("retry_after", 5.0))
                    if not math.isfinite(retry_after) or retry_after < 0: retry_after = 5.0
                except Exception:
                    pass
                return False, f"Rate limited (429), retry after {retry_after}s", retry_after

            if 400 <= resp.status_code < 500:
                # Client permanent error (e.g. 401 unauthorized, 404 not found)
                return False, f"permanent_http_{resp.status_code}", None

            # 5xx Server error: Discord or an intermediary may have already accepted and posted the message.
            # Default to holding as delivery_unknown to prevent duplicate messages unless retry_on_5xx is explicitly enabled.
            if self.retry_on_5xx:
                return False, f"Discord server error HTTP {resp.status_code}", 5.0
            return False, "delivery_unknown", None

        except requests.exceptions.Timeout:
            return False, "delivery_unknown", None
        except requests.exceptions.RequestException:
            return False, "delivery_unknown", None
