import logging

from aiogram import Bot

from config import settings
from funpay.types import FPOrder

logger = logging.getLogger(__name__)


class NotificationService:
    def __init__(self, bot: Bot):
        self._bot = bot

    async def notify_help_command(
        self, chat_id: int, buyer_name: str, message_text: str
    ) -> None:
        text = (
            f"Покупатель просит помощи!\n\n"
            f"<b>Покупатель:</b> {buyer_name}\n"
            f"<b>Сообщение:</b> {message_text}\n"
            f"<b>Чат FunPay:</b> https://funpay.com/chat/?node={chat_id}"
        )
        await self._send(text)

    async def notify_new_order(self, order: FPOrder) -> None:
        text = (
            f"Новый заказ!\n\n"
            f"<b>ID:</b> {order.id}\n"
            f"<b>Покупатель:</b> {order.buyer_username}\n"
            f"<b>Сумма:</b> {order.price} {order.currency}\n"
            f"<b>Описание:</b> {order.description}"
        )
        await self._send(text)

    async def notify_order_status_changed(
        self, order_id: str, old_status: str, new_status: str
    ) -> None:
        text = (
            f"Статус заказа изменён\n\n"
            f"<b>Заказ:</b> {order_id}\n"
            f"<b>Статус:</b> {old_status} → {new_status}"
        )
        await self._send(text)

    async def notify_dispute(self, order_id: str, buyer: str, chat_id: int) -> None:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        text = (
            f"⚠️ <b>Открыт спор по заказу</b>\n\n"
            f"<b>Заказ:</b> {order_id}\n"
            f"<b>Покупатель:</b> {buyer}\n\n"
            f"Аренда автоматически приостановлена.\n"
            f"Выберите действие:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Кикнуть и завершить",
                                  callback_data=f"dispute_kick:{order_id}")],
            [InlineKeyboardButton(text="🔗 Открыть чат FunPay",
                                  url=f"https://funpay.com/chat/?node={chat_id}")],
            [InlineKeyboardButton(text="✅ Закрыть как возврат",
                                  callback_data=f"dispute_refund:{order_id}")],
        ])
        try:
            await self._bot.send_message(
                settings.TELEGRAM_ADMIN_ID, text, parse_mode="HTML", reply_markup=kb
            )
        except Exception as e:
            logger.error("Failed to send dispute alert: %s", e)

    async def notify_session_terminated(self, username: str, success: bool) -> None:
        if success:
            text = f"Сессия завершена: <b>{username}</b>"
        else:
            text = f"Не удалось завершить сессию: <b>{username}</b>. Требуется ручное действие."
        await self._send(text)

    async def notify_auth_error(self, message: str) -> None:
        text = f"Ошибка авторизации FunPay!\n{message}\nОбновите PHPSESSID в .env и перезапустите бота."
        await self._send(text)

    _warned_chat_not_found = False

    async def _send(self, text: str) -> None:
        try:
            await self._bot.send_message(
                settings.TELEGRAM_ADMIN_ID, text, parse_mode="HTML"
            )
        except Exception as e:
            err = str(e)
            if "chat not found" in err.lower() and not NotificationService._warned_chat_not_found:
                NotificationService._warned_chat_not_found = True
                logger.error(
                    "Telegram says 'chat not found'. Two possible reasons:\n"
                    "  1) Your TELEGRAM_ADMIN_ID=%s is incorrect — verify via @userinfobot\n"
                    "  2) You haven't started a chat with the bot yet — open the bot in Telegram "
                    "and send /start (bots can't initiate conversations)",
                    settings.TELEGRAM_ADMIN_ID,
                )
            else:
                logger.error("Failed to send Telegram notification: %s", e)
