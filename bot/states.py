from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    username = State()
    password = State()
    mfa = State()       # only if Riot demanded MFA


class SetCookies(StatesGroup):
    # Re-purposed for Phase 2: re-authenticate an existing account
    # (carries target account_id in context)
    mfa = State()
