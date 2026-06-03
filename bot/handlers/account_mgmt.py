"""Telegram FSM handlers for adding accounts and managing tokens (Phase 2)."""
import io
import logging

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards.admin_kb import account_detail_kb, cancel_kb, main_menu_kb
from bot.states import AddAccount, SetCookies
from config import settings
from db.database import AsyncSessionLocal
from db.models import AccountState
from db.repositories.account_repo import AccountRepository
from riot.rso_auth import (
    AuthError,
    CaptchaRequired,
    InvalidCredentials,
    MfaRequired,
    RateLimited,
    RsoAuth,
)

logger = logging.getLogger(__name__)
router = Router()


class AdminFilter(Filter):
    async def __call__(self, event) -> bool:
        return event.from_user.id == settings.TELEGRAM_ADMIN_ID


router.message.filter(AdminFilter())
router.callback_query.filter(AdminFilter())


# ---------------------------------------------------------------- cancel
@router.callback_query(F.data == "fsm_cancel")
async def cb_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Отменено.", reply_markup=main_menu_kb())
    await callback.answer()


# ---------------------------------------------------------------- add account
@router.callback_query(F.data == "account_add")
async def cb_account_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddAccount.username)
    await callback.message.edit_text(
        "<b>Добавление аккаунта</b>\n\n"
        "Шаг 1/2. Отправьте Riot username "
        "(только имя для входа, без #TAG):",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(AddAccount.username)
async def add_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip()
    if not username:
        await message.answer("Пусто. Введите username ещё раз:", reply_markup=cancel_kb())
        return
    await state.update_data(username=username)
    await state.set_state(AddAccount.password)
    await message.answer(
        "Шаг 2/2. Отправьте пароль от аккаунта.\n"
        "Бот сразу авторизуется и сохранит токены — больше ничего вводить не нужно.",
        reply_markup=cancel_kb(),
    )


@router.message(AddAccount.password)
async def add_password(message: Message, state: FSMContext) -> None:
    password = (message.text or "").strip()
    if not password:
        await message.answer("Пусто. Введите пароль ещё раз:", reply_markup=cancel_kb())
        return
    await state.update_data(password=password)
    # Try to authenticate immediately
    await message.answer("⌛ Авторизация в Riot...")
    await _try_login_and_save(message, state, new=True)


@router.message(AddAccount.mfa)
async def add_mfa(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if not code.isdigit() or len(code) not in (4, 6):
        await message.answer("Код должен быть из 4 или 6 цифр. Введите ещё раз:",
                             reply_markup=cancel_kb())
        return
    await state.update_data(mfa_code=code)
    await message.answer("⌛ Подтверждаю код...")
    await _try_login_and_save(message, state, new=True)


# ---------------------------------------------------------------- relogin existing
@router.callback_query(F.data.startswith("setcookies:"))
async def cb_relogin_start(callback: CallbackQuery, state: FSMContext) -> None:
    """Re-authenticate an existing account (refresh tokens). The button is
    still called 'setcookies:*' for backward compat with the keyboard."""
    account_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        account = await repo.get_by_id(account_id)
        if not account:
            await callback.answer("Аккаунт не найден.", show_alert=True)
            return

    await state.set_state(SetCookies.mfa)
    await state.update_data(account_id=account_id)
    await callback.message.edit_text(
        f"<b>Перелогин аккаунта {account.riot_username}</b>\n\n"
        f"Бот авторизуется по сохранённым логину/паролю.\n"
        f"Если включена 2FA — Riot пришлёт код на почту, введите его сюда.\n"
        f"Если 2FA нет — просто отправьте «-» (минус).",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.message(SetCookies.mfa)
async def relogin_with_optional_mfa(message: Message, state: FSMContext) -> None:
    code = (message.text or "").strip()
    if code in ("-", "—", "нет", "no"):
        code = None
    elif not code.isdigit() or len(code) not in (4, 6):
        await message.answer("Код должен быть из 4 или 6 цифр, либо «-» если 2FA выключена.",
                             reply_markup=cancel_kb())
        return

    data = await state.get_data()
    account_id = data["account_id"]
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        account = await repo.get_by_id(account_id)
        if not account:
            await state.clear()
            await message.answer("Аккаунт не найден.", reply_markup=main_menu_kb())
            return
        await state.update_data(
            username=account.riot_username, password=account.riot_password,
            mfa_code=code,
        )

    await message.answer("⌛ Авторизация...")
    await _try_login_and_save(message, state, new=False)


# ---------------------------------------------------------------- core
async def _try_login_and_save(message: Message, state: FSMContext, new: bool) -> None:
    """Run RSO auth, save tokens. Handles MFA branching."""
    data = await state.get_data()
    username = data["username"]
    password = data["password"]
    mfa_code = data.get("mfa_code")

    auth = RsoAuth()
    try:
        tokens = await auth.authenticate(username, password, mfa_code=mfa_code)
    except MfaRequired as e:
        # Ask user for the code, stay in FSM
        target_state = AddAccount.mfa if new else SetCookies.mfa
        await state.set_state(target_state)
        await message.answer(
            f"🔐 Включена 2FA. Riot отправил код на {e.email_hint}.\n"
            f"Введите код (4 или 6 цифр):",
            reply_markup=cancel_kb(),
        )
        return
    except InvalidCredentials:
        await state.clear()
        await message.answer(
            "❌ Неверный логин или пароль. Попробуйте /start → ➕ Добавить аккаунт заново.",
            reply_markup=main_menu_kb(),
        )
        return
    except RateLimited:
        await state.clear()
        await message.answer(
            "⏳ Riot временно ограничил вход (rate limit). Попробуйте через 10-30 минут.",
            reply_markup=main_menu_kb(),
        )
        return
    except CaptchaRequired:
        await state.clear()
        await message.answer(
            "🤖 Riot показал CAPTCHA — для этого аккаунта требуется anti-captcha "
            "(2captcha API). Свяжитесь с разработчиком для подключения.",
            reply_markup=main_menu_kb(),
        )
        return
    except AuthError as e:
        await state.clear()
        await message.answer(f"❌ Ошибка авторизации: <code>{e}</code>",
                             reply_markup=main_menu_kb())
        return

    # Save (create or update)
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        existing = await repo.get_by_username(username)
        if existing:
            existing.riot_password = password
            if existing.state in (AccountState.FREE, AccountState.LOCKED,
                                  AccountState.MFA_REQUIRED):
                existing.state = AccountState.READY
            await session.commit()
            acc_id = existing.id
        else:
            acc = await repo.create(riot_username=username, riot_password=password)
            acc.state = AccountState.READY
            await session.commit()
            acc_id = acc.id

        await repo.save_tokens(acc_id, tokens)

    await state.clear()
    await message.answer(
        f"✅ <b>{username}</b> авторизован.\n"
        f"PUUID: <code>{tokens.puuid[:8]}...</code>\n"
        f"Регион: {tokens.region}\n"
        f"Аккаунт в состоянии READY — готов к аренде.",
        reply_markup=main_menu_kb(),
    )
