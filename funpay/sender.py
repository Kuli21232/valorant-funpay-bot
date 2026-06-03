import logging

from config import settings
from funpay.client import FunPayClient

logger = logging.getLogger(__name__)


class FunPaySender:
    async def send(self, client: FunPayClient, chat_id: int, text: str) -> None:
        try:
            await client.send_chat_message(chat_id, text)
            logger.info("Sent message to chat %d", chat_id)
        except Exception as e:
            logger.error("Failed to send message to chat %d: %s", chat_id, e)

    async def send_auto_reply(self, client: FunPayClient, chat_id: int) -> None:
        await self.send(client, chat_id, settings.AUTO_REPLY_TEXT)

    async def send_help_acknowledgement(self, client: FunPayClient, chat_id: int) -> None:
        await self.send(client, chat_id, "Продавец уведомлён, ожидайте ответа.")
