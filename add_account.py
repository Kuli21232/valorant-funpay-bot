"""CLI для добавления / управления Valorant-аккаунтами (без Telegram).

Использует Riot RSO Auth напрямую — поэтому работает с обычным интернетом
(без датацентрового VPN, который Riot блокирует с auth_failure).

Запускается через add_account.bat.
"""
from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

from utils.logging_config import setup_logging

setup_logging()

from db.database import AsyncSessionLocal, init_db
from db.models import AccountState
from db.repositories.account_repo import AccountRepository
from riot.api_client import RiotApiClient
from riot.rso_auth import (
    AuthError,
    CaptchaRequired,
    InvalidCredentials,
    MfaRequired,
    RateLimited,
    RsoAuth,
)


# --------------------------------------------------------------------- helpers

def _read_password(prompt: str = "Password: ") -> str:
    """Read password VISIBLY by default.
    Windows getpass strips some special chars silently → leads to bogus auth_failure.
    Visible input is reliable. The console is local, no security risk."""
    pw = input(prompt + "(виден на экране, чтобы не было опечаток) ").strip()
    if pw:
        print(f"   [длина пароля: {len(pw)} символов]")
    return pw


async def _do_auth_with_mfa(username: str, password: str) -> "RiotTokens | None":
    """Run RSO auth, prompt for MFA code if needed. Returns tokens or None on failure."""
    auth = RsoAuth()
    try:
        return await auth.authenticate(username, password)
    except MfaRequired as e:
        print()
        print(f"🔐 Включена 2FA. Riot отправил код на {e.email_hint}")
        code = input("Введите код из почты: ").strip()
        if not code:
            print("[!] Пустой код, отмена.")
            return None
        try:
            return await auth.authenticate(username, password, mfa_code=code)
        except AuthError as e2:
            print(f"[!] Не прошло после MFA: {e2}")
            return None
    except InvalidCredentials:
        print()
        print("❌ Riot не принял логин/пароль (auth_failure).")
        print()
        print("Что проверить:")
        print()
        print("  1. ОПЕЧАТКА В ПАРОЛЕ — самая частая причина.")
        print("     Прямо сейчас откройте https://account.riotgames.com в Chrome,")
        print("     попробуйте войти ТЕМ ЖЕ паролем ВРУЧНУЮ. Если не входит — пароль")
        print("     неверный, восстановите через 'Forgot password'.")
        print()
        print("  2. КАПЧА / RQDATA GRABBER — Riot может маскировать captcha-fail")
        print("     как auth_failure. Проверьте:")
        print("     • CAPSOLVER_KEY в .env + баланс на capsolver.com")
        print("     • RIOT_SKIP_RQDATA=True в .env (отключает headless Chromium)")
        print()
        print("  3. КУЛДАУН АККАУНТА — если этот аккаунт сегодня прошёл через")
        print("     серию fail-логинов, Riot временно блокирует вход на 6-24 часа.")
        print("     Решение: возьмите СВЕЖИЙ аккаунт.")
        print()
        print("  4. VPN — должен быть выключен. Проверьте ifconfig.me, страна=Россия.")
        return None
    except RateLimited:
        print("⏳ Rate limit. Подождите 30-60 минут.")
        return None
    except CaptchaRequired as e:
        print()
        print(f"🤖 Captcha problem: {e}")
        print()
        print("Что проверить:")
        print("  • CAPSOLVER_KEY в .env (рекомендуется — Riot's hCaptcha Enterprise)")
        print("  • Баланс на capsolver.com / 2captcha.com")
        print("  • Если 2captcha таймаутит — поставьте CAPSOLVER_KEY, он надёжнее")
        return None
    except AuthError as e:
        print(f"❌ Ошибка авторизации: {e}")
        print()
        print("Дополнительные шаги:")
        print("  • Если видите invalid_request + country=rus —")
        print("    Riot блокирует API-логин с этого IP. Нужен RIOT_PROXY")
        print("    (резидентский прокси: BrightData / Smartproxy / IPRoyal)")
        print("  • Запустите catch_auth.bat — перехватите реальный cURL из Chrome,")
        print("    это покажет точный endpoint и headers которые использует Riot.")
        print("  • Попробуйте RIOT_SKIP_RQDATA=True в .env")
        return None


# --------------------------------------------------------------------- actions

async def add() -> int:
    print()
    print("=" * 60)
    print("  Добавление аккаунта (через Riot API напрямую)")
    print("=" * 60)
    print()
    print("Скрипт работает с обычным интернетом — VPN можно отключить.")
    print("Бот сразу логинится в Riot, получает токены и сохраняет в БД.")
    print()

    username = input("Riot username: ").strip()
    if not username:
        print("Отмена.")
        return 1
    password = _read_password()
    if not password:
        print("Отмена.")
        return 1

    print()
    print(f"Введено: username={username!r}, длина пароля={len(password)}")
    confirm = input("Всё правильно? (Y/n): ").strip().lower()
    if confirm == "n":
        print("Отмена.")
        return 1

    print()
    print("⌛ Авторизация в Riot...")
    tokens = await _do_auth_with_mfa(username, password)
    if not tokens:
        return 1

    await init_db()
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
            print(f"[OK] Аккаунт {username} обновлён (id={acc_id}).")
        else:
            acc = await repo.create(riot_username=username, riot_password=password)
            acc.state = AccountState.READY
            await session.commit()
            acc_id = acc.id
            print(f"[OK] Аккаунт {username} добавлен (id={acc_id}).")

        await repo.save_tokens(acc_id, tokens)

    print()
    print("=" * 60)
    print("✅ УСПЕХ")
    print(f"  username: {username}")
    print(f"  region:   {tokens.region}")
    print(f"  puuid:    {tokens.puuid}")
    print(f"  expires:  {tokens.expires_at} UTC")
    print(f"  ssid TTL: ~1 год (refresh-токен)")
    print(f"  state:    READY")
    print("=" * 60)
    print()
    print("Аккаунт готов к аренде через бота.")
    return 0


async def list_accounts() -> int:
    await init_db()
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        accounts = await repo.get_all()
    print()
    if not accounts:
        print("Аккаунтов в базе нет.")
        return 0
    print(f"Всего аккаунтов: {len(accounts)}")
    print("-" * 70)
    for a in accounts:
        region = a.rso_region or "?"
        last = a.last_login_at.strftime('%d.%m %H:%M') if a.last_login_at else "—"
        print(f"  [{a.id}] {a.riot_username:<20} {a.rank_display:<12} "
              f"state={a.state:<10} region={region:<4} login={last}")
    return 0


async def delete_account() -> int:
    await init_db()
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        accounts = await repo.get_all()
        if not accounts:
            print("Аккаунтов нет.")
            return 0
        for a in accounts:
            print(f"  [{a.id}] {a.riot_username} (state={a.state})")
        try:
            aid = int(input("\nID аккаунта для удаления: ").strip())
        except ValueError:
            print("Неверный ID.")
            return 1
        acc = await repo.get_by_id(aid)
        if not acc:
            print("Не найден.")
            return 1
        confirm = input(f"Удалить {acc.riot_username}? (y/N): ").strip().lower()
        if confirm != "y":
            print("Отмена.")
            return 0
        await repo.delete(aid)
        print(f"[OK] Удалён {acc.riot_username}")
    return 0


async def relogin_account() -> int:
    """Перевыпустить токены для существующего аккаунта."""
    await init_db()
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        accounts = await repo.get_all()
        if not accounts:
            print("Аккаунтов нет.")
            return 0
        for a in accounts:
            print(f"  [{a.id}] {a.riot_username} (state={a.state}, region={a.rso_region or '?'})")
        try:
            aid = int(input("\nID аккаунта: ").strip())
        except ValueError:
            print("Неверный ID.")
            return 1
        acc = await repo.get_by_id(aid)
        if not acc:
            print("Не найден.")
            return 1
        username = acc.riot_username
        password = acc.riot_password

    print(f"\n⌛ Перелогин {username}...")
    tokens = await _do_auth_with_mfa(username, password)
    if not tokens:
        return 1

    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        await repo.save_tokens(aid, tokens)
        if acc.state in (AccountState.FREE, AccountState.LOCKED,
                         AccountState.MFA_REQUIRED):
            await repo.set_state(aid, AccountState.READY)

    print(f"[OK] Сессия обновлена. PUUID={tokens.puuid[:8]}..., region={tokens.region}")
    return 0


async def check_ban() -> int:
    """Проверить аккаунт на бан через Riot account-standing API."""
    await init_db()
    async with AsyncSessionLocal() as session:
        repo = AccountRepository(session)
        accounts = await repo.get_all()
        if not accounts:
            print("Аккаунтов нет.")
            return 0
        for a in accounts:
            print(f"  [{a.id}] {a.riot_username} (state={a.state})")
        try:
            aid = int(input("\nID аккаунта: ").strip())
        except ValueError:
            print("Неверный ID.")
            return 1
        account = await repo.get_by_id(aid)
        if not account:
            print("Не найден.")
            return 1

    print(f"\n⌛ Проверка статуса {account.riot_username}...")
    try:
        standing = await RiotApiClient().check_ban(account)
    except Exception as e:
        print(f"[!] Ошибка: {e}")
        return 1

    if standing.banned:
        print(f"🚫 Аккаунт в бане: {standing.reason}")
        async with AsyncSessionLocal() as session:
            repo = AccountRepository(session)
            await repo.mark_banned(aid, reason=standing.reason)
        print("    Помечен как BANNED — исключён из выдачи.")
    else:
        print(f"🟢 Санкций не обнаружено.")
    return 0


# --------------------------------------------------------------------- main

async def main() -> int:
    print()
    print("=" * 60)
    print("  Управление аккаунтами Valorant FunPay Bot")
    print("=" * 60)
    print()
    print("Меню:")
    print("  1) Добавить аккаунт (логин в Riot)")
    print("  2) Показать все аккаунты")
    print("  3) Удалить аккаунт")
    print("  4) Перелогин аккаунта (обновить токены)")
    print("  5) Проверить бан")
    print("  0) Выход")
    choice = input("\nВыбор: ").strip()
    if choice == "1":
        return await add()
    if choice == "2":
        return await list_accounts()
    if choice == "3":
        return await delete_account()
    if choice == "4":
        return await relogin_account()
    if choice == "5":
        return await check_ban()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(1)
