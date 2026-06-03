"""Async IMAP client to fetch 2FA codes from Riot emails.

Built for firstmail.ltd (provider for cheap Riot mailboxes) but works with
any standard IMAP4 SSL server (Gmail/Yandex/Outlook all use the same
protocol — only host/port differ).

Usage:
    async with ImapClient(host, port, login, password) as ic:
        code = await ic.wait_for_riot_mfa_code(timeout=120)
        # → "123456"
"""
from __future__ import annotations

import asyncio
import email
import logging
import re
import ssl
from dataclasses import dataclass
from email.header import decode_header
from email.message import Message
from typing import Optional

import aioimaplib

logger = logging.getLogger(__name__)

# Common Riot sender patterns. Riot uses several depending on region/product.
_RIOT_SENDERS = (
    "noreply@mail.accounts.riotgames.com",
    "noreply@accounts.riotgames.com",
    "noreply@riotgames.com",
)

# Riot MFA codes are typically 6 digits in the subject or body.
# Subject ex: "Riot Games verification: 123456"
# Body ex: "Your verification code is: 123456"
_CODE_PATTERNS = [
    re.compile(r"\b(\d{6})\b"),      # 6-digit code (most common)
    re.compile(r"\b(\d{4})\b"),      # 4-digit code (some flows)
]


@dataclass
class ImapMessage:
    uid: int
    from_addr: str
    subject: str
    body: str

    def find_code(self) -> Optional[str]:
        """Extract a numeric MFA code from subject + body."""
        haystack = f"{self.subject}\n{self.body}"
        for pat in _CODE_PATTERNS:
            m = pat.search(haystack)
            if m:
                return m.group(1)
        return None


class ImapClient:
    """Async IMAP client over SSL. Auto-reconnects on connection drops."""

    def __init__(
        self,
        host: str,
        port: int = 993,
        login: str = "",
        password: str = "",
        ssl_enabled: bool = True,
    ):
        self.host = host
        self.port = port
        self.login = login
        self.password = password
        self.ssl_enabled = ssl_enabled
        self._client: Optional[aioimaplib.IMAP4_SSL | aioimaplib.IMAP4] = None

    async def __aenter__(self) -> "ImapClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.disconnect()

    async def connect(self) -> None:
        if self.ssl_enabled:
            ssl_ctx = ssl.create_default_context()
            self._client = aioimaplib.IMAP4_SSL(
                host=self.host, port=self.port, ssl_context=ssl_ctx, timeout=30
            )
        else:
            self._client = aioimaplib.IMAP4(host=self.host, port=self.port, timeout=30)
        await self._client.wait_hello_from_server()
        r = await self._client.login(self.login, self.password)
        if r.result != "OK":
            raise IOError(f"IMAP login failed: {r}")
        logger.info("IMAP connected: %s@%s", self.login, self.host)

    async def disconnect(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.logout()
        except Exception:
            pass
        self._client = None

    async def select_inbox(self) -> None:
        assert self._client is not None
        await self._client.select("INBOX")

    async def search_recent(self, max_count: int = 20) -> list[ImapMessage]:
        """Get the last `max_count` messages from INBOX."""
        assert self._client is not None
        await self.select_inbox()
        # ALL returns space-separated UIDs as bytes
        r = await self._client.search("ALL")
        if r.result != "OK":
            return []
        uids = (r.lines[0].decode() if r.lines else "").split()
        if not uids:
            return []
        uids = uids[-max_count:]  # newest last in IMAP
        messages: list[ImapMessage] = []
        for uid_s in reversed(uids):  # newest first in our list
            uid = int(uid_s)
            msg = await self._fetch(uid)
            if msg:
                messages.append(msg)
        return messages

    async def _fetch(self, uid: int) -> Optional[ImapMessage]:
        assert self._client is not None
        r = await self._client.fetch(str(uid), "(RFC822)")
        if r.result != "OK" or not r.lines:
            return None
        raw = b""
        for ln in r.lines:
            if isinstance(ln, (bytes, bytearray)) and len(ln) > 200:
                raw = bytes(ln)
                break
        if not raw:
            # Fallback — last large chunk
            big = [ln for ln in r.lines if isinstance(ln, (bytes, bytearray))]
            if big:
                raw = bytes(big[-1])
        try:
            msg: Message = email.message_from_bytes(raw)
        except Exception:
            return None
        return ImapMessage(
            uid=uid,
            from_addr=_decode(msg.get("From", "")),
            subject=_decode(msg.get("Subject", "")),
            body=_get_body(msg),
        )

    async def wait_for_riot_mfa_code(
        self, since_uid: int = 0, timeout: int = 120, poll_interval: int = 5
    ) -> Optional[str]:
        """Poll the inbox until a new Riot email with an MFA code arrives.

        since_uid — ignore messages with UID <= this value (call get_max_uid()
                    first to mark a baseline before triggering the login).
        timeout   — total seconds to wait before giving up.
        Returns the code as a string, or None on timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                messages = await self.search_recent(max_count=15)
            except Exception as e:
                logger.warning("IMAP search error: %s — reconnecting", e)
                try:
                    await self.disconnect()
                    await self.connect()
                except Exception as e2:
                    logger.error("IMAP reconnect failed: %s", e2)
                    return None
                continue
            for m in messages:
                if m.uid <= since_uid:
                    continue
                if not _is_riot(m.from_addr):
                    continue
                code = m.find_code()
                if code:
                    logger.info("Riot MFA code found in uid=%d: %s", m.uid, code)
                    return code
            await asyncio.sleep(poll_interval)
        logger.warning("Timed out waiting for Riot MFA email")
        return None

    async def get_max_uid(self) -> int:
        """Return the highest UID currently in INBOX. Use this as baseline
        BEFORE triggering an action that should produce a new email."""
        assert self._client is not None
        await self.select_inbox()
        r = await self._client.search("ALL")
        if r.result != "OK" or not r.lines:
            return 0
        uids = (r.lines[0].decode() if r.lines else "").split()
        return max((int(u) for u in uids), default=0)


# ----- helpers ---------------------------------------------------------------

def _decode(s: str) -> str:
    if not s:
        return ""
    parts = decode_header(s)
    out = []
    for txt, enc in parts:
        if isinstance(txt, bytes):
            try:
                out.append(txt.decode(enc or "utf-8", errors="replace"))
            except Exception:
                out.append(txt.decode("utf-8", errors="replace"))
        else:
            out.append(txt)
    return "".join(out)


def _get_body(msg: Message) -> str:
    """Concatenate text/plain and text/html parts (HTML kept as-is for regex)."""
    chunks: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        chunks.append(payload.decode(charset, errors="replace"))
                    except Exception:
                        chunks.append(payload.decode("utf-8", errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                chunks.append(payload.decode(charset, errors="replace"))
            except Exception:
                chunks.append(payload.decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def _is_riot(from_addr: str) -> bool:
    low = from_addr.lower()
    return any(s in low for s in _RIOT_SENDERS) or "riotgames" in low
