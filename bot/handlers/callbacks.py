import logging

from aiogram import Router
from aiogram.filters import Filter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from datetime import datetime

from bot.keyboards.admin_kb import (
    account_detail_kb,
    accounts_list_kb,
    back_kb,
    cancel_kb,
    confirm_terminate_all_kb,
    main_menu_kb,
)
from bot.states import SetCookies
from config import settings
from db.database import AsyncSessionLocal
from db.models import AccountState, OrderStatus, RentalEndReason
from db.repositories.account_repo import AccountRepository
from db.repositories.buyer_repo import BuyerRepository
from db.repositories.order_repo import OrderRepository
from db.repositories.rental_repo import RentalRepository

logger = logging.getLogger(__name__)
router = Router()


class AdminFilter(Filter):
    async def __call__(self, callback: CallbackQuery) -> bool:
        return callback.from_user.id == settings.TELEGRAM_ADMIN_ID


router.callback_query.filter(AdminFilter())


@router.callback_query(lambda c: c.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "Valorant FunPay Bot\nВыберите действие:",
        reply_markup=main_menu_kb(),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "accounts_list")
async def cb_accounts_list(callback: CallbackQuery) -> None:
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        accounts = await repo.get_all()

    if not accounts:
        await callback.answer("Аккаунты не найдены. Добавьте их в базу данных.", show_alert=True)
        return

    await callback.message.edit_text(
        "Все аккаунты:",
        reply_markup=accounts_list_kb(accounts),
    )
    await callback.answer()


_STATE_DESC = {
    AccountState.FREE: "🔴 свободен (не залогинен)",
    AccountState.READY: "🟢 готов к аренде (токены активны)",
    AccountState.RENTED: "🟡 арендован",
    AccountState.LOCKED: "⚫ заблокирован ботом (ошибка, нужна проверка)",
    AccountState.BANNED: "🚫 в бане Riot (не выдаётся)",
    AccountState.MFA_REQUIRED: "🔐 нужен MFA-код (Перелогин)",
}


@router.callback_query(lambda c: c.data and c.data.startswith("account:"))
async def cb_account_detail(callback: CallbackQuery) -> None:
    account_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        account_repo = AccountRepository(session)
        rental_repo = RentalRepository(session)
        account = await account_repo.get_by_id(account_id)
        active_rental = None
        if account and account.state == AccountState.RENTED:
            active_rental = await rental_repo.get_active_by_account(account_id)

    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    text = (
        f"<b>Аккаунт:</b> {account.riot_username}\n"
        f"<b>Состояние:</b> {_STATE_DESC.get(account.state, account.state)}\n"
        f"<b>Ранг:</b> {account.rank_display}"
    )
    if account.rso_region:
        text += f"\n<b>Регион:</b> {account.rso_region}"
    if account.rso_puuid:
        text += f"\n<b>PUUID:</b> <code>{account.rso_puuid[:8]}...</code>"
    if account.last_login_at:
        text += f"\n<b>Последний логин:</b> {account.last_login_at.strftime('%d.%m %H:%M')} UTC"
    if account.state == AccountState.BANNED and account.ban_reason:
        text += f"\n\n🚫 <b>Бан:</b> {account.ban_reason}"
    if active_rental:
        remaining = active_rental.ends_at - datetime.utcnow()
        mins = max(0, int(remaining.total_seconds() // 60))
        text += (
            f"\n\n<b>Текущая аренда:</b>\n"
            f"Покупатель: {active_rental.buyer_username}\n"
            f"Заказ: {active_rental.order_funpay_id}\n"
            f"Осталось: {mins} мин"
        )

    await callback.message.edit_text(text, reply_markup=account_detail_kb(account_id))
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("status:"))
async def cb_status(callback: CallbackQuery) -> None:
    account_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        account = await repo.get_by_id(account_id)

    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    desc = _STATE_DESC.get(account.state, account.state)
    await callback.answer(f"{account.riot_username}: {desc}", show_alert=True)


@router.callback_query(lambda c: c.data and c.data.startswith("rank:"))
async def cb_rank(callback: CallbackQuery) -> None:
    account_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        account = await repo.get_by_id(account_id)

    if not account:
        await callback.answer("Аккаунт не найден.", show_alert=True)
        return

    await callback.answer(
        f"{account.riot_username}\nРанг: {account.rank_display}", show_alert=True
    )


@router.callback_query(lambda c: c.data and c.data.startswith("dispute_kick:"))
async def cb_dispute_kick(callback: CallbackQuery) -> None:
    funpay_order_id = callback.data.split(":", 1)[1]
    await callback.answer("Кикаю...")
    async with AsyncSessionLocal() as session:
        order_repo = OrderRepository(session)
        account_repo = AccountRepository(session)
        rental_repo = RentalRepository(session)
        order = await order_repo.get_by_funpay_id(funpay_order_id)
        if not order or not order.account_id:
            await callback.message.answer("Заказ не найден или к нему не привязан аккаунт.")
            return
        account = await account_repo.get_by_id(order.account_id)

    from riot.api_client import RiotApiClient
    try:
        result = await RiotApiClient().terminate(account)
    except Exception as e:
        await callback.message.answer(f"❌ Ошибка кика: <code>{e}</code>")
        return

    async with AsyncSessionLocal() as session:
        account_repo = AccountRepository(session)
        rental_repo = RentalRepository(session)
        if result and result.success and result.new_password:
            await account_repo.update_password(account.id, result.new_password)
            await account_repo.set_state(account.id, AccountState.READY)
        else:
            await account_repo.set_state(account.id, AccountState.LOCKED)
        active = await rental_repo.get_active_by_account(account.id)
        if active:
            await rental_repo.close(active.id, RentalEndReason.MANUAL_KICK)

    if result and result.success:
        await callback.message.answer(
            f"✅ Спор по {funpay_order_id}: аккаунт <b>{account.riot_username}</b> "
            f"кикнут, пароль обновлён."
        )
    else:
        err = result.error if result else "неизвестная ошибка"
        await callback.message.answer(f"❌ Не удалось кикнуть: <code>{err}</code>")


@router.callback_query(lambda c: c.data and c.data.startswith("dispute_refund:"))
async def cb_dispute_refund(callback: CallbackQuery) -> None:
    funpay_order_id = callback.data.split(":", 1)[1]
    async with AsyncSessionLocal() as session:
        order_repo = OrderRepository(session)
        rental_repo = RentalRepository(session)
        account_repo = AccountRepository(session)
        order = await order_repo.get_by_funpay_id(funpay_order_id)
        if not order:
            await callback.answer("Заказ не найден.", show_alert=True)
            return
        await order_repo.update_status(funpay_order_id, OrderStatus.CANCELLED)
        if order.account_id:
            active = await rental_repo.get_active_by_account(order.account_id)
            if active:
                await rental_repo.close(active.id, RentalEndReason.ERROR)
            await account_repo.set_state(order.account_id, AccountState.READY)

    await callback.message.answer(
        f"✅ Заказ <b>{funpay_order_id}</b> закрыт как возврат.\n"
        f"Возврат денежных средств — выполните вручную на FunPay."
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("checkban:"))
async def cb_check_ban(callback: CallbackQuery) -> None:
    account_id = int(callback.data.split(":")[1])
    await callback.answer("Проверяю статус...")
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        account = await repo.get_by_id(account_id)
        if not account:
            await callback.message.answer("Аккаунт не найден.")
            return

    from riot.api_client import RiotApiClient
    try:
        standing = await RiotApiClient().check_ban(account)
    except Exception as e:
        await callback.message.answer(f"❌ Не удалось проверить: <code>{e}</code>")
        return

    if standing.banned:
        async with AsyncSessionLocal() as session:
            repo = AccountRepository(session)
            await repo.mark_banned(account_id, reason=standing.reason)
        await callback.message.answer(
            f"🚫 <b>{account.riot_username}</b> в бане.\n"
            f"Причина: {standing.reason}\n"
            f"Состояние: BANNED — исключён из выдачи."
        )
    else:
        await callback.message.answer(
            f"🟢 <b>{account.riot_username}</b> — санкций не обнаружено."
        )


@router.callback_query(lambda c: c.data and c.data.startswith("kick:"))
async def cb_kick(callback: CallbackQuery) -> None:
    account_id = int(callback.data.split(":")[1])
    await callback.answer("Запускаю завершение сессии...")

    async with AsyncSessionLocal() as session:
        account_repo = AccountRepository(session)
        account = await account_repo.get_by_id(account_id)
        if not account:
            await callback.message.answer("Аккаунт не найден.")
            return
        username = account.riot_username

    await callback.message.answer(
        f"Завершаю сессию для <b>{username}</b> (смена пароля)..."
    )

    from riot.api_client import RiotApiClient
    try:
        result = await RiotApiClient().terminate(account)
    except Exception as e:
        logger.exception("Kick failed for %s: %s", username, e)
        result = None

    success = bool(result and result.success)

    async with AsyncSessionLocal() as session:
        account_repo = AccountRepository(session)
        rental_repo = RentalRepository(session)

        if success:
            await account_repo.update_password(account_id, result.new_password)
            active_rental = await rental_repo.get_active_by_account(account_id)
            if active_rental:
                await rental_repo.close(active_rental.id, RentalEndReason.MANUAL_KICK)
            await account_repo.set_state(account_id, AccountState.READY)
            await callback.message.answer(
                f"✅ Сессия для <b>{username}</b> завершена. "
                f"Пароль обновлён, аккаунт снова READY.",
                reply_markup=accounts_list_kb(await account_repo.get_all()),
            )
        else:
            err = result.error if result else "unknown error"
            await callback.message.answer(
                f"❌ Не удалось завершить сессию для <b>{username}</b>.\n"
                f"Причина: <code>{err}</code>\n\n"
                f"Возможные шаги:\n"
                f"• Обновите токены через карточку аккаунта → 🔑 Перелогин\n"
                f"• Проверьте логи (строки [API])."
            )

    logger.info("Kick %s: success=%s", username, success)


@router.callback_query(lambda c: c.data and c.data.startswith("relogin:"))
async def cb_relogin(callback: CallbackQuery, state: FSMContext) -> None:
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
        f"<b>Перелогин {account.riot_username}</b> (API, без браузера)\n\n"
        f"Если включена 2FA — Riot пришлёт код на почту, введите его сюда.\n"
        f"Если 2FA нет — просто отправьте «-» (минус).",
        reply_markup=cancel_kb(),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "rentals_active")
async def cb_rentals_active(callback: CallbackQuery) -> None:
    async with AsyncSessionLocal() as session:
        rental_repo = RentalRepository(session)
        account_repo = AccountRepository(session)
        rentals = await rental_repo.get_active()
        if not rentals:
            await callback.message.edit_text(
                "Активных аренд нет.",
                reply_markup=back_kb("main_menu"),
            )
            await callback.answer()
            return

        lines = ["<b>Активные аренды:</b>", ""]
        now = datetime.utcnow()
        for r in rentals:
            account = await account_repo.get_by_id(r.account_id)
            uname = account.riot_username if account else f"acc#{r.account_id}"
            remaining = r.ends_at - now
            mins = int(remaining.total_seconds() // 60)
            status = f"{mins} мин" if mins > 0 else "ИСТЕКАЕТ"
            lines.append(
                f"• <b>{uname}</b> ← {r.buyer_username}\n"
                f"  Осталось: {status} (заказ {r.order_funpay_id})"
            )

    await callback.message.edit_text(
        "\n".join(lines), reply_markup=back_kb("main_menu")
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "buyers_top")
async def cb_buyers_top(callback: CallbackQuery) -> None:
    async with AsyncSessionLocal() as session:
        repo = BuyerRepository(session)
        top = await repo.top(limit=10)

    if not top:
        await callback.message.edit_text(
            "Статистика покупателей пока пуста.",
            reply_markup=back_kb("main_menu"),
        )
        await callback.answer()
        return

    lines = ["<b>Топ покупателей:</b>", ""]
    for i, b in enumerate(top, 1):
        last = b.last_order_at.strftime("%d.%m") if b.last_order_at else "—"
        lines.append(
            f"{i}. <b>{b.username}</b>\n"
            f"   {b.total_orders} заказ(ов), "
            f"{b.total_minutes_rented} мин, "
            f"{b.total_spent:.0f} ₽ (последний {last})"
        )

    await callback.message.edit_text(
        "\n".join(lines), reply_markup=back_kb("main_menu")
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("delacc:"))
async def cb_delete_account(callback: CallbackQuery) -> None:
    account_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        account = await repo.get_by_id(account_id)
        if not account:
            await callback.answer("Аккаунт не найден.", show_alert=True)
            return
        username = account.riot_username
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да, удалить", callback_data=f"delacc_confirm:{account_id}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"account:{account_id}"),
    ]])
    await callback.message.edit_text(
        f"Удалить аккаунт <b>{username}</b> из базы?", reply_markup=kb
    )
    await callback.answer()


@router.callback_query(lambda c: c.data and c.data.startswith("delacc_confirm:"))
async def cb_delete_account_confirm(callback: CallbackQuery) -> None:
    account_id = int(callback.data.split(":")[1])
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        account = await repo.get_by_id(account_id)
        name = account.riot_username if account else "?"
        await repo.delete(account_id)
        accounts = await repo.get_all()
    await callback.message.edit_text(
        f"🗑 Аккаунт <b>{name}</b> удалён.",
        reply_markup=accounts_list_kb(accounts),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "terminate_all")
async def cb_terminate_all(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "Вы уверены? Это завершит сессии ВСЕХ аккаунтов.",
        reply_markup=confirm_terminate_all_kb(),
    )
    await callback.answer()


@router.callback_query(lambda c: c.data == "terminate_all_confirm")
async def cb_terminate_all_confirm(callback: CallbackQuery) -> None:
    await callback.answer("Запускаю завершение всех сессий...")
    await callback.message.edit_text("Завершаю все сессии, подождите...")

    async with AsyncSessionLocal() as session:
        account_repo = AccountRepository(session)
        accounts = await account_repo.get_all()

    if not accounts:
        await callback.message.answer("Нет аккаунтов для завершения.")
        return

    # Terminate each account via pure-API (no browser, no windows)
    from riot.api_client import RiotApiClient
    api = RiotApiClient()
    results: dict[str, bool] = {}
    for account in accounts:
        uname = account.riot_username
        try:
            res = await api.terminate(account)
            results[uname] = res.success
            if res.success and res.new_password:
                async with AsyncSessionLocal() as session:
                    account_repo = AccountRepository(session)
                    await account_repo.update_password(account.id, res.new_password)
        except Exception as e:
            logger.exception("Terminate-all failed for %s: %s", uname, e)
            results[uname] = False

    async with AsyncSessionLocal() as session:
        account_repo = AccountRepository(session)
        rental_repo = RentalRepository(session)
        for account in accounts:
            if results.get(account.riot_username):
                active_rental = await rental_repo.get_active_by_account(account.id)
                if active_rental:
                    await rental_repo.close(active_rental.id, RentalEndReason.GLOBAL_KICK)
                await account_repo.set_state(account.id, AccountState.READY)

    lines = [
        f"{'✅' if ok else '❌'} {uname}" for uname, ok in results.items()
    ]
    summary = "\n".join(lines) if lines else "Нет результатов"
    await callback.message.answer(f"Результаты завершения сессий:\n{summary}")
