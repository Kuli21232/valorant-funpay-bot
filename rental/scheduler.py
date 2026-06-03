"""Background task that monitors active rentals and kicks expired buyers.

Kick mechanism (Phase 2): change the Riot password via API. This invalidates
every active session of the account — same effect as 'Sign out everywhere'
but achievable with normal-tier RSO tokens (no browser, no captcha)."""
import asyncio
import logging
from datetime import datetime

from bot.services.notification_service import NotificationService
from db.database import AsyncSessionLocal
from db.models import AccountState, RentalEndReason
from db.repositories.account_repo import AccountRepository
from db.repositories.buyer_repo import BuyerRepository
from db.repositories.rental_repo import RentalRepository
from funpay.client import FunPayClient
from funpay.sender import FunPaySender
from riot.api_client import RiotApiClient

logger = logging.getLogger(__name__)


class RentalScheduler:
    CHECK_INTERVAL_SECONDS = 30  # check every 30s

    def __init__(
        self,
        funpay_client: FunPayClient,
        funpay_sender: FunPaySender,
        notifier: NotificationService,
    ):
        self._fp_client = funpay_client
        self._fp_sender = funpay_sender
        self._notifier = notifier
        self._riot = RiotApiClient()

    async def run(self) -> None:
        logger.info("Rental scheduler started")
        while True:
            try:
                await self._tick()
                await self._auto_unban_tick()
            except Exception as e:
                logger.exception("Scheduler error: %s", e)
            await asyncio.sleep(self.CHECK_INTERVAL_SECONDS)

    async def _tick(self) -> None:
        async with AsyncSessionLocal() as session:
            rental_repo = RentalRepository(session)
            expired = await rental_repo.get_expired()

        for rental in expired:
            logger.info(
                "Rental %d expired (account_id=%d, buyer=%s) — kicking",
                rental.id, rental.account_id, rental.buyer_username,
            )
            await self._expire_rental(rental.id)

    async def _auto_unban_tick(self) -> None:
        """Move BANNED accounts back to READY once their ban window is over."""
        async with AsyncSessionLocal() as session:
            account_repo = AccountRepository(session)
            banned = await account_repo.get_by_state(AccountState.BANNED)
            now = datetime.utcnow()
            for acc in banned:
                if acc.banned_until and acc.banned_until <= now:
                    logger.info("Auto-unban %s (banned_until passed)", acc.riot_username)
                    await account_repo.set_state(acc.id, AccountState.READY)
                    acc.banned_until = None
                    acc.ban_reason = None
                    await session.commit()

    async def _expire_rental(self, rental_id: int) -> None:
        async with AsyncSessionLocal() as session:
            rental_repo = RentalRepository(session)
            account_repo = AccountRepository(session)
            rental = await rental_repo.get_by_id(rental_id)
            if rental is None or rental.ended_at is not None:
                return
            account = await account_repo.get_by_id(rental.account_id)
            if account is None:
                return
            username = account.riot_username
            chat_id = rental.chat_id
            buyer_funpay_id = rental.buyer_funpay_id
            buyer_username = rental.buyer_username
            minutes = max(1, int((rental.ends_at - rental.started_at).total_seconds() // 60))
            price = rental.price or 0.0

        # Kick via password change (invalidates all sessions instantly)
        try:
            result = await self._riot.terminate(account)
        except Exception as e:
            logger.exception("API terminate failed for %s: %s", username, e)
            result = None

        success = bool(result and result.success)

        async with AsyncSessionLocal() as session:
            rental_repo = RentalRepository(session)
            account_repo = AccountRepository(session)
            buyer_repo = BuyerRepository(session)

            await rental_repo.close(rental_id, RentalEndReason.TIME_EXPIRED)
            if success and result.new_password:
                await account_repo.update_password(rental.account_id, result.new_password)
                await account_repo.set_state(rental.account_id, AccountState.READY)
            else:
                await account_repo.set_state(rental.account_id, AccountState.LOCKED)
            await buyer_repo.record_rental(
                funpay_id=buyer_funpay_id,
                username=buyer_username,
                minutes=minutes,
                price=price,
            )

        # Notify buyer in FunPay
        try:
            await self._fp_sender.send(
                self._fp_client, chat_id,
                f"Время аренды истекло. Сессия завершена. Спасибо за заказ!"
            )
        except Exception as e:
            logger.warning("Could not message buyer in FunPay: %s", e)

        # Notify admin
        try:
            if success:
                await self._notifier._send(
                    f"✅ Аренда завершена по таймеру: <b>{username}</b>\n"
                    f"Покупатель: {buyer_username} ({minutes} мин)\n"
                    f"Пароль обновлён, аккаунт снова READY."
                )
            else:
                err = result.error if result else "unknown error"
                await self._notifier._send(
                    f"⚠️ Не удалось кикнуть <b>{username}</b> по таймеру.\n"
                    f"Причина: {err}\n"
                    f"Аккаунт переведён в LOCKED."
                )
        except Exception as e:
            logger.warning("Could not notify admin: %s", e)
