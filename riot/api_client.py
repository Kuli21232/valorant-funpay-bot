"""Riot API operations using RSO tokens (Phase 2 — no browser, no cookies).

Two main operations:
  • terminate(account)  — kick the renter by changing the password
  • check_ban(account)  — fetch account-standing to detect bans

Both use a fresh access_token, refreshed automatically from the stored ssid.
"""
from __future__ import annotations

import logging
import secrets
import string
from dataclasses import dataclass
from typing import Optional

from db.models import ValorantAccount
from riot.rso_auth import (
    AuthError,
    RiotTokens,
    RsoAuth,
    TokensExpired,
    _make_session,
)

logger = logging.getLogger(__name__)


# Endpoint references
_PASSWORD_URL = "https://account.riotgames.com/api/account/v1/user/password"
_STANDING_URL = "https://account.riotgames.com/api/v1/player/account-standing"


@dataclass
class KickResult:
    success: bool
    new_password: Optional[str] = None
    error: Optional[str] = None


@dataclass
class AccountStanding:
    banned: bool
    raw: dict
    reason: str = ""


class RiotApiClient:
    """High-level Riot API client used by the bot."""

    def __init__(self):
        self._auth = RsoAuth()

    # ----- token management -----

    async def get_fresh_tokens(self, account: ValorantAccount) -> RiotTokens:
        """Always return a non-expired RiotTokens for this account.
        Uses ssid refresh; falls back to username/password if ssid is dead."""
        if account.rso_ssid:
            try:
                tokens = await self._auth.refresh(account.rso_ssid)
                return tokens
            except (TokensExpired, AuthError) as e:
                logger.warning("[API] ssid refresh failed for %s: %s — falling back to password",
                               account.riot_username, e)
        # Full password auth
        return await self._auth.authenticate(
            account.riot_username, account.riot_password
        )

    # ----- main operations -----

    async def terminate(self, account: ValorantAccount) -> KickResult:
        """Kick the renter by changing the account password.

        Riot invalidates all sessions when the password changes — same effect
        as 'Sign out everywhere' but achievable with normal-tier tokens.
        The new password is stored in the DB and returned, so the bot can
        deliver it to the next renter.
        """
        try:
            tokens = await self.get_fresh_tokens(account)
        except AuthError as e:
            return KickResult(success=False, error=f"auth failed: {e}")

        new_password = _gen_password()
        async with _make_session() as sess:
            try:
                async with sess.put(
                    _PASSWORD_URL,
                    json={
                        "currentPassword": account.riot_password,
                        "password": new_password,
                    },
                    headers={
                        "Authorization": f"Bearer {tokens.access_token}",
                        "X-Riot-Entitlements-JWT": tokens.entitlement_token,
                    },
                ) as r:
                    status = r.status
                    body = await r.text()
            except Exception as e:
                return KickResult(success=False, error=f"http error: {e}")

        ok = 200 <= status < 300
        logger.info("[API] %s password change → %s | body=%s",
                    account.riot_username, status, (body or "")[:200])
        if not ok:
            return KickResult(success=False,
                              error=f"HTTP {status}: {body[:200]}")
        return KickResult(success=True, new_password=new_password)

    async def check_ban(self, account: ValorantAccount) -> AccountStanding:
        """Fetch the account's standing — detect bans/restrictions.

        Returns AccountStanding(banned=True/False, raw=full_json).
        Banned accounts should be excluded from rental rotation.
        """
        tokens = await self.get_fresh_tokens(account)
        async with _make_session() as sess:
            async with sess.get(
                _STANDING_URL,
                headers={
                    "Authorization": f"Bearer {tokens.access_token}",
                    "X-Riot-Entitlements-JWT": tokens.entitlement_token,
                },
            ) as r:
                status = r.status
                if status != 200:
                    text = await r.text()
                    logger.warning("[API] standing for %s returned %s: %s",
                                   account.riot_username, status, (text or "")[:200])
                    return AccountStanding(banned=False, raw={})
                data = await r.json()
        # Standing response shape: {"penalties": [{"type": "..", "expiry": ".."}]}
        penalties = data.get("penalties") or []
        active = [p for p in penalties if not p.get("expired")]
        banned = bool(active)
        reason = ""
        if banned:
            p = active[0]
            reason = f"{p.get('type', 'ban')} until {p.get('expiry', '?')}"
        logger.info("[API] %s standing: banned=%s, %d active penalty(ies)",
                    account.riot_username, banned, len(active))
        return AccountStanding(banned=banned, raw=data, reason=reason)


# ----- helpers ---------------------------------------------------------------

def _gen_password(length: int = 16) -> str:
    """Generate a Riot-compliant random password.
    Riot requires: 8-128 chars, with letters + digits at minimum. We include
    a special char too to be future-proof, and use URL-safe alphabet to avoid
    quoting issues when delivering through FunPay chat.
    """
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.islower() for c in pw)
                and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                and any(c in "!@#$%^&*" for c in pw)):
            return pw
