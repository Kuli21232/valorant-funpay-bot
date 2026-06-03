from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db.models import AccountState, ValorantAccount


_STATE_LABELS = {
    AccountState.FREE: "🔴 free",
    AccountState.READY: "🟢 ready",
    AccountState.RENTED: "🟡 rented",
    AccountState.LOCKED: "⚫ locked",
    AccountState.BANNED: "🚫 banned",
    AccountState.MFA_REQUIRED: "🔐 нужен MFA",
}


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="account_add")],
            [InlineKeyboardButton(text="Все аккаунты", callback_data="accounts_list")],
            [InlineKeyboardButton(text="Активные аренды", callback_data="rentals_active")],
            [InlineKeyboardButton(text="Статистика покупателей", callback_data="buyers_top")],
            [InlineKeyboardButton(text="Завершить все сессии", callback_data="terminate_all")],
        ]
    )


def accounts_list_kb(accounts: list[ValorantAccount]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for acc in accounts:
        label = _STATE_LABELS.get(acc.state, acc.state)
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{acc.riot_username} {label}",
                    callback_data=f"account:{acc.id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="← Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def account_detail_kb(account_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Кик (смена пароля)", callback_data=f"kick:{account_id}")],
            [InlineKeyboardButton(text="🔑 Перелогин", callback_data=f"setcookies:{account_id}")],
            [InlineKeyboardButton(text="🔍 Проверить бан", callback_data=f"checkban:{account_id}")],
            [InlineKeyboardButton(text="Статус", callback_data=f"status:{account_id}")],
            [InlineKeyboardButton(text="Чек ранга", callback_data=f"rank:{account_id}")],
            [InlineKeyboardButton(text="🗑 Удалить аккаунт", callback_data=f"delacc:{account_id}")],
            [InlineKeyboardButton(text="← К списку", callback_data="accounts_list")],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="✖ Отмена", callback_data="fsm_cancel")]]
    )


def back_kb(target: str = "main_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="← Назад", callback_data=target)]]
    )


def confirm_terminate_all_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, завершить все", callback_data="terminate_all_confirm"),
                InlineKeyboardButton(text="Отмена", callback_data="main_menu"),
            ]
        ]
    )
