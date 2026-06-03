"""Handle incoming QR-code images from buyers in FunPay chat.

End-to-end flow:
  1. Buyer sends a screenshot of the Valorant QR-login screen into chat.
  2. The poller emits a `new_message` event whose FPMessage has image_urls.
  3. EventDispatcher routes images that belong to an active rental's chat
     into QrPipeline.handle_image().
  4. Pipeline downloads the image, decodes QR, calls
     MobileBackend.approve_buyer_qr() — which (eventually) approves the
     sign-in via the bot's Riot Mobile session.
  5. Buyer gets confirmation in chat.
"""
from __future__ import annotations

import logging

from bot.services.notification_service import NotificationService
from db.database import AsyncSessionLocal
from db.repositories.account_repo import AccountRepository
from db.repositories.rental_repo import RentalRepository
from funpay.client import FunPayClient
from funpay.sender import FunPaySender
from funpay.types import FPMessage
from mobile.factory import get_mobile_backend
from utils.qr_decoder import decode_all_from_bytes

logger = logging.getLogger(__name__)


class QrPipeline:
    """Owns the QR-from-chat → Mobile.approve_buyer_qr flow."""

    def __init__(
        self,
        fp_client: FunPayClient,
        fp_sender: FunPaySender,
        notifier: NotificationService,
    ):
        self._fp_client = fp_client
        self._fp_sender = fp_sender
        self._notifier = notifier
        self._mobile = get_mobile_backend()

    async def handle_message(self, msg: FPMessage) -> bool:
        """Inspect a chat message and process any attached QR codes.

        Returns True if we recognized & handled a QR; False otherwise."""
        if not msg.image_urls:
            return False
        if not msg.is_incoming:
            return False

        # Find an active rental tied to this chat (by buyer chat_id)
        async with AsyncSessionLocal() as session:
            rental_repo = RentalRepository(session)
            actives = await rental_repo.get_active()
        rental = next((r for r in actives if r.chat_id == msg.chat_id), None)
        if rental is None:
            logger.debug("QR image in chat %d but no active rental — ignore",
                         msg.chat_id)
            return False

        # Load account
        async with AsyncSessionLocal() as session:
            acc_repo = AccountRepository(session)
            account = await acc_repo.get_by_id(rental.account_id)
        if account is None:
            logger.warning("Active rental %d points at missing account %d",
                           rental.id, rental.account_id)
            return False

        logger.info(
            "Processing %d image(s) from buyer %s for rental %d (account %s)",
            len(msg.image_urls), msg.chat_name, rental.id, account.riot_username,
        )

        any_qr = False
        for url in msg.image_urls:
            qr_payload = await self._decode_qr_from_url(url)
            if not qr_payload:
                continue
            any_qr = True
            logger.info("QR decoded for rental %d: %s...", rental.id, qr_payload[:60])

            # Approve via mobile backend
            try:
                ok = await self._mobile.approve_buyer_qr(account.id, qr_payload)
            except Exception as e:
                logger.exception("mobile.approve_buyer_qr failed: %s", e)
                ok = False

            if ok:
                await self._fp_sender.send(
                    self._fp_client, msg.chat_id,
                    "✅ Вход подтверждён. Запустите игру — вы залогинены.\n"
                    f"Аккаунт работает до {rental.ends_at.strftime('%H:%M')} UTC."
                )
                await self._notifier._send(
                    f"✅ Покупатель {rental.buyer_username} вошёл по QR в "
                    f"<b>{account.riot_username}</b> (аренда #{rental.id})"
                )
                return True
            else:
                await self._fp_sender.send(
                    self._fp_client, msg.chat_id,
                    "❌ Не удалось подтвердить вход. Возможно QR устарел "
                    "(они живут ~3 минуты). Попробуйте сгенерировать новый "
                    "и пришлите снова."
                )
                await self._notifier._send(
                    f"⚠️ QR от {rental.buyer_username} не подтвердился "
                    f"(аккаунт {account.riot_username}, аренда #{rental.id})"
                )
                return False

        if not any_qr:
            # Attachments were present but none decoded as QR
            await self._fp_sender.send(
                self._fp_client, msg.chat_id,
                "На скриншоте не удалось распознать QR-код. "
                "Сделайте чёткий снимок QR (без обрезки краёв) и пришлите снова."
            )
        return any_qr

    async def _decode_qr_from_url(self, url: str) -> str | None:
        """Download image from FunPay and decode QR(s)."""
        try:
            data = await self._fp_client.download_image(url)
        except Exception as e:
            logger.warning("Failed to download %s: %s", url, e)
            return None
        codes = decode_all_from_bytes(data)
        return codes[0] if codes else None
