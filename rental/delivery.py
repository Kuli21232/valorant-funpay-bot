"""Assign a READY account to a paid order and deliver credentials to the buyer."""
import logging
from typing import Optional

from bot.services.notification_service import NotificationService
from db.database import AsyncSessionLocal
from db.models import AccountState
from db.repositories.account_repo import AccountRepository
from db.repositories.buyer_repo import BuyerRepository
from db.repositories.order_repo import OrderRepository
from db.repositories.rental_repo import RentalRepository
from funpay.client import FunPayClient
from funpay.sender import FunPaySender
from funpay.types import FPOrder
from rental.duration_parser import DEFAULT_MINUTES, parse_duration_minutes

logger = logging.getLogger(__name__)


class RentalDeliveryService:
    def __init__(
        self,
        fp_client: FunPayClient,
        fp_sender: FunPaySender,
        notifier: NotificationService,
    ):
        self._fp_client = fp_client
        self._fp_sender = fp_sender
        self._notifier = notifier

    async def deliver(self, order: FPOrder) -> bool:
        """Try to auto-assign a free account to the order and deliver credentials.
        Returns True if delivered, False if no account available / manual needed."""

        minutes = parse_duration_minutes(order.description or "")
        if minutes is None:
            logger.warning(
                "Could not parse rental duration from order %s: %r — using default %d min",
                order.id, order.description, DEFAULT_MINUTES,
            )
            minutes = DEFAULT_MINUTES

        async with AsyncSessionLocal() as session:
            account_repo = AccountRepository(session)
            order_repo = OrderRepository(session)
            rental_repo = RentalRepository(session)
            buyer_repo = BuyerRepository(session)

            # Already delivered?
            db_order = await order_repo.get_by_funpay_id(order.id)
            if db_order and db_order.delivered:
                logger.debug("Order %s already delivered", order.id)
                return True

            # Find a ready account
            account = await account_repo.get_first_ready()
            if account is None:
                logger.warning("No READY accounts available for order %s", order.id)
                await self._notifier._send(
                    f"⚠️ Новый заказ {order.id} от {order.buyer_username}, "
                    f"но <b>нет свободных аккаунтов</b> в состоянии READY.\n"
                    f"Добавьте аккаунт через бота."
                )
                return False

        # ----- Ban check BEFORE committing rental -----
        try:
            from riot.api_client import RiotApiClient
            standing = await RiotApiClient().check_ban(account)
        except Exception as e:
            logger.warning("Ban-check failed for %s (%s) — proceeding anyway", account.riot_username, e)
            standing = None

        if standing and standing.banned:
            async with AsyncSessionLocal() as session:
                account_repo = AccountRepository(session)
                await account_repo.mark_banned(account.id, reason=standing.reason)
            logger.warning("Account %s is BANNED (%s) — re-rolling delivery",
                           account.riot_username, standing.reason)
            await self._notifier._send(
                f"🚫 Аккаунт <b>{account.riot_username}</b> в бане ({standing.reason}).\n"
                f"Переведён в BANNED, не выдаётся. Пробую другой аккаунт для заказа {order.id}."
            )
            # Recurse — try with another free account
            return await self.deliver(order)

        async with AsyncSessionLocal() as session:
            account_repo = AccountRepository(session)
            order_repo = OrderRepository(session)
            rental_repo = RentalRepository(session)
            buyer_repo = BuyerRepository(session)

            # Re-load account inside fresh session
            account = await account_repo.get_by_id(account.id)
            if not account:
                return False

            # Create rental session
            rental = await rental_repo.create(
                account_id=account.id,
                buyer_funpay_id=order.buyer_id,
                buyer_username=order.buyer_username,
                order_funpay_id=order.id,
                chat_id=order.chat_id,
                rental_minutes=minutes,
                price=order.price,
            )
            await account_repo.set_state(account.id, AccountState.RENTED)
            await order_repo.mark_delivered(order.id, account.id)
            await buyer_repo.upsert(order.buyer_id, order.buyer_username)

            account_username = account.riot_username
            account_password = account.riot_password
            ends_at = rental.ends_at

        # Deliver QR-flow instruction (no login/password — buyer signs in
        # via QR scan, bot approves via Mobile)
        delivery_text = (
            f"Здравствуйте! Аренда на {minutes} мин.\n\n"
            f"📱 Как войти:\n"
            f"1. Откройте Valorant на ПК\n"
            f"2. На экране входа выберите «Войти с помощью QR-кода»\n"
            f"3. На экране появится QR-код\n"
            f"4. Сделайте СКРИНШОТ этого QR и отправьте его сюда в чат\n\n"
            f"После получения скрина бот за 5-10 секунд подтвердит вход.\n"
            f"Время окончания аренды: {ends_at.strftime('%H:%M')} UTC.\n\n"
            f"Если возникнут проблемы — напишите !помощь"
        )
        try:
            await self._fp_sender.send(self._fp_client, order.chat_id, delivery_text)
        except Exception as e:
            logger.exception("Failed to send QR instruction to chat %d: %s", order.chat_id, e)

        # Notify admin
        await self._notifier._send(
            f"✅ Заказ {order.id} назначен (ждём QR от покупателя):\n"
            f"Аккаунт: <b>{account_username}</b>\n"
            f"Покупатель: {order.buyer_username}\n"
            f"Длительность: {minutes} мин\n"
            f"Окончание: {ends_at.strftime('%H:%M')} UTC"
        )

        logger.info("Delivered QR instruction to order %s → account %s for %d min",
                    order.id, account_username, minutes)
        return True
