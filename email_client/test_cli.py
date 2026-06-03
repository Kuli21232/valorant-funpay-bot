"""CLI test for the IMAP client.

Usage: test_email.bat
  1) Подключение к ящику + чтение последних писем
  2) Ожидание MFA-кода от Riot (запустите login на Riot ПК
     ВО ВРЕМЯ выполнения скрипта — он поймает свежее письмо).
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

from email_client.imap_client import ImapClient


# Default for firstmail.ltd
DEFAULT_HOST = "imap.firstmail.ltd"
DEFAULT_PORT = 993


async def test_connection() -> int:
    print()
    host = input(f"IMAP host [{DEFAULT_HOST}]: ").strip() or DEFAULT_HOST
    try:
        port = int(input(f"IMAP port [{DEFAULT_PORT}]: ").strip() or DEFAULT_PORT)
    except ValueError:
        port = DEFAULT_PORT
    login = input("Login (full email): ").strip()
    password = input("Password: ").strip()
    if not login or not password:
        print("Отмена.")
        return 1

    print()
    print(f"⌛ Подключение к {host}:{port}...")
    try:
        async with ImapClient(host, port, login, password) as ic:
            print(f"✅ Подключено как {login}")
            print()

            print("--- Последние 10 писем ---")
            messages = await ic.search_recent(max_count=10)
            for m in messages:
                marker = "🎯 RIOT" if "riotgames" in m.from_addr.lower() else "      "
                preview = (m.subject or "")[:60]
                print(f"  {marker}  uid={m.uid:6}  from={m.from_addr[:40]!s:40}  {preview!r}")
                code = m.find_code()
                if code:
                    print(f"            → Найден возможный код: {code}")

            if not messages:
                print("  (ящик пустой)")

            print()
            wait = input("Ждать новое письмо от Riot? (y/N): ").strip().lower()
            if wait == "y":
                baseline = await ic.get_max_uid()
                print(f"⌛ Ожидание нового письма от Riot (UID > {baseline})...")
                print("Запустите вход на account.riotgames.com в браузере СЕЙЧАС.")
                code = await ic.wait_for_riot_mfa_code(
                    since_uid=baseline, timeout=180, poll_interval=5
                )
                if code:
                    print(f"\n✅ MFA код найден: {code}")
                else:
                    print("\n[!] Не дождались письма за 3 минуты.")
    except Exception as e:
        print(f"❌ Ошибка: {e}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(test_connection()))
    except KeyboardInterrupt:
        print("\nПрервано.")
        sys.exit(1)
