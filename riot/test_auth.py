"""Quick CLI tool to test Riot auth for a single account.

Run: test_auth.bat
Enter username + password and watch the raw Riot response.
Helps isolate per-account / per-IP rate-locks from real bugs.
"""
from __future__ import annotations

import asyncio
import getpass
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

from utils.logging_config import setup_logging
setup_logging()

from riot.rso_auth import (
    AuthError,
    InvalidCredentials,
    MfaRequired,
    RateLimited,
    RsoAuth,
)


async def main() -> int:
    print()
    print("=" * 60)
    print("  Диагностика Riot RSO Auth")
    print("=" * 60)
    print()
    print("Этот скрипт пробует войти в указанный аккаунт через API Riot")
    print("(тот же путь, что использует бот) и показывает подробный ответ.")
    print()

    username = input("Username: ").strip()
    if not username:
        return 1
    password = getpass.getpass("Password (не отображается): ").strip() or input(
        "Password (видимый ввод): "
    ).strip()
    if not password:
        return 1

    print()
    print("→ Отправляю запрос в Riot...")
    print()

    auth = RsoAuth()
    try:
        tokens = await auth.authenticate(username, password)
        print()
        print("=" * 60)
        print("✅ УСПЕХ")
        print(f"  region:   {tokens.region}")
        print(f"  puuid:    {tokens.puuid}")
        print(f"  expires:  {tokens.expires_at}")
        print(f"  ssid:     {tokens.ssid[:30]}...")
        print("=" * 60)
        return 0
    except MfaRequired as e:
        print(f"\n🔐 Требуется MFA-код (отправлен на {e.email_hint})")
        code = input("Введите код из почты: ").strip()
        try:
            tokens = await auth.authenticate(username, password, mfa_code=code)
            print(f"\n✅ УСПЕХ после MFA. region={tokens.region}, puuid={tokens.puuid}")
            return 0
        except Exception as e2:
            print(f"\n❌ MFA провалился: {e2}")
            return 1
    except InvalidCredentials:
        print()
        print("=" * 60)
        print("❌ AUTH_FAILURE")
        print("=" * 60)
        print("Возможные причины:")
        print("  1. Неверный пароль (опечатка)")
        print("  2. Капча решена неверно / не решена → Riot маскирует как auth_failure")
        print("     Проверьте CAPSOLVER_KEY в .env и баланс на capsolver.com")
        print("  3. Headless Playwright (rqdata grabber) отравляет сессию/IP")
        print("     Попробуйте: RIOT_SKIP_RQDATA=True в .env")
        print("  4. Аккаунт временно заблокирован Riot после серии fail-логинов")
        print("     (обычно лочится на 6-24 часа, потом сам отойдёт)")
        print("  5. IP попал в блок-лист Riot (datacenter VPN отметался)")
        print()
        print("Проверьте:")
        print("  - Войдите в браузере на ТОМ ЖЕ IP/VPN что и бот сейчас")
        print("    (если в браузере с другим IP — сравнение некорректное)")
        print("  - Попробуйте RIOT_SKIP_RQDATA=True + CAPSOLVER_KEY")
        print("  - Попробуйте через час с новым аккаунтом")
        print("  - Попробуйте без VPN / с другим провайдером")
        return 1
    except RateLimited as e:
        print(f"\n⏳ Rate limit: {e}")
        return 1
    except AuthError as e:
        print()
        print("=" * 60)
        print("❌ Ошибка:", e)
        print("=" * 60)
        print()
        print("Дополнительные шаги:")
        print("  • Если видите invalid_request + country=rus —")
        print("    Riot блокирует API-логин с этого IP. Нужен RIOT_PROXY")
        print("    (резидентский прокси: BrightData / Smartproxy / IPRoyal)")
        print("  • Запустите catch_auth.bat — перехватите реальный cURL из Chrome,")
        print("    это покажет, какой endpoint и headers использует Riot прямо сейчас.")
        print("  • Попробуйте RIOT_SKIP_RQDATA=True в .env")
        return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(1)
