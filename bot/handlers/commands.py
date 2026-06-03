from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.keyboards.admin_kb import main_menu_kb
from config import settings

router = Router()


def is_admin(user_id: int) -> bool:
    return user_id == settings.TELEGRAM_ADMIN_ID


@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext) -> None:
    if not is_admin(message.from_user.id):
        await message.answer("Доступ запрещён.")
        return
    await state.clear()  # reset any in-progress FSM (add account / set cookies)
    await message.answer(
        "Valorant FunPay Bot\nВыберите действие:",
        reply_markup=main_menu_kb(),
    )
