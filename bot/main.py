import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers import account_mgmt, callbacks, commands
from config import settings

logger = logging.getLogger(__name__)


async def create_bot() -> tuple[Bot, Dispatcher]:
    session = None
    if settings.TELEGRAM_PROXY:
        session = AiohttpSession(proxy=settings.TELEGRAM_PROXY)
        logger.info("Telegram bot using proxy")

    bot = Bot(
        token=settings.TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session,
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(commands.router)
    # account_mgmt handles FSM (add account / set cookies) — register before
    # callbacks so its specific callback_data (account_add, setcookies:*) win.
    dp.include_router(account_mgmt.router)
    dp.include_router(callbacks.router)
    return bot, dp
