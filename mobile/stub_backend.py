"""Stub mobile backend — used while the real Android emulator / device
isn't wired up yet. Lets the rest of the bot run end-to-end (rental
delivery pipeline, etc.) without needing a working mobile session."""
from __future__ import annotations

import logging
from typing import Callable, Optional

from mobile.interface import MobileBackend, MobileSessionInfo

logger = logging.getLogger(__name__)


class StubMobileBackend(MobileBackend):
    @property
    def name(self) -> str:
        return "stub"

    async def ensure_logged_in(
        self, account_id: int, username: str, password: str,
        email_mfa_callback: Optional[Callable] = None,
    ) -> MobileSessionInfo:
        logger.info("[mobile/stub] ensure_logged_in(%d, %s) — no-op",
                    account_id, username)
        return MobileSessionInfo(account_id=account_id, is_logged_in=True)

    async def enable_qr_signin(self, account_id: int) -> bool:
        logger.info("[mobile/stub] enable_qr_signin(%d) — pretending OK", account_id)
        return True

    async def approve_buyer_qr(self, account_id: int, qr_payload: str) -> bool:
        logger.info("[mobile/stub] approve_buyer_qr(%d) payload=%r — pretending OK",
                    account_id, qr_payload[:80])
        return True

    async def status(self, account_id: int) -> MobileSessionInfo:
        return MobileSessionInfo(account_id=account_id, is_logged_in=True)

    async def logout(self, account_id: int) -> bool:
        logger.info("[mobile/stub] logout(%d)", account_id)
        return True
