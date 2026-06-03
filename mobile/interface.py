"""Interface for Riot Mobile App control.

Three implementations are planned (selected via .env MOBILE_PROVIDER):
  - stub:     does nothing real, returns successful stubs. For dev/tests.
  - emulator: drives an Android emulator (LDPlayer/Genymotion) via Appium.
  - device:   physical Android phone over ADB.

The bot consumes only this interface, so swapping the backend later only
requires changing the provider — no rewrites of the rental pipeline.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional


@dataclass
class MobileSessionInfo:
    """Snapshot of a bot's mobile session for a Riot account."""
    account_id: int
    is_logged_in: bool
    needs_attention: bool = False
    error: Optional[str] = None


class MobileBackend(abc.ABC):
    """Abstract operations the bot needs from a Mobile-controlled session."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Backend name for logs / DB."""

    @abc.abstractmethod
    async def ensure_logged_in(
        self, account_id: int, username: str, password: str,
        email_mfa_callback: Optional[callable] = None,
    ) -> MobileSessionInfo:
        """Make sure the bot's Riot Mobile is signed in to this account.

        email_mfa_callback(): async callable() → str | None — fetches the
        MFA code from the account's mailbox if Riot prompts for one."""

    @abc.abstractmethod
    async def enable_qr_signin(self, account_id: int) -> bool:
        """Scan the QR shown on account.riotgames.com / security to pair the
        bot's mobile device. Returns True on success."""

    @abc.abstractmethod
    async def approve_buyer_qr(self, account_id: int, qr_payload: str) -> bool:
        """Approve a buyer's Valorant 'Sign in with QR' request.
        qr_payload is the raw string read from the QR image."""

    @abc.abstractmethod
    async def status(self, account_id: int) -> MobileSessionInfo:
        """Return the current session snapshot. Cheap (no Riot API call)."""

    @abc.abstractmethod
    async def logout(self, account_id: int) -> bool:
        """Log out of Riot Mobile (used when retiring an account)."""


class MobileError(Exception):
    """Generic error raised by mobile backends."""


class MobileNotConfigured(MobileError):
    """Selected backend isn't set up (e.g. no emulator running)."""


class MobileMfaRequired(MobileError):
    """Email-based MFA is required and the caller didn't supply a callback."""
