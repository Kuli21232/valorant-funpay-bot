"""Interactive setup wizard — generates .env from user input."""
from __future__ import annotations

import sys
from pathlib import Path

# Force UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

ENV_PATH = Path(__file__).parent / ".env"


def ask(prompt: str, default: str | None = None, required: bool = True) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return default
        if value:
            return value
        if not required:
            return ""
        print("  ! Поле обязательно к заполнению.")


def ask_int(prompt: str, default: int | None = None) -> int:
    while True:
        raw = ask(prompt, str(default) if default is not None else None)
        try:
            return int(raw)
        except ValueError:
            print("  ! Введите целое число.")


def main() -> int:
    print()
    print("=" * 60)
    print("  Мастер настройки Valorant FunPay Bot")
    print("=" * 60)
    print()
    print("Заполните параметры. Все значения будут сохранены в .env")
    print()

    print("--- Telegram ---")
    print("Получить токен: напишите @BotFather в Telegram → /newbot")
    tg_token = ask("Telegram Bot Token")
    print()
    print("Узнать свой ID: напишите @userinfobot в Telegram")
    tg_admin = ask_int("Telegram Admin ID (ваш ID)")
    print()

    print("--- FunPay ---")
    print("Cookies можно скопировать из браузера (F12 → Application → Cookies → funpay.com)")
    fp_golden = ask("FunPay golden_key cookie")
    fp_phpsess = ask("FunPay PHPSESSID cookie")
    print()
    print("ID продавца: цифры из URL https://funpay.com/users/XXXXXX/")
    fp_user_id = ask_int("FunPay User ID (числовой)")
    print()

    print("--- Прокси (для Telegram в РФ) ---")
    print("Если вы в России, Telegram API заблокирован — нужен прокси.")
    print("Форматы: http://user:pass@host:port  или  socks5://user:pass@host:port")
    print("Если прокси не нужен — просто нажмите Enter.")
    tg_proxy = ask("Прокси для Telegram (опционально)", default="", required=False)
    fp_proxy = ask("Прокси для FunPay (опционально, обычно не нужен)", default="", required=False)
    print()

    print("--- Прочее ---")
    poll_interval = ask_int("Интервал опроса FunPay (сек)", default=15)
    auto_reply = ask(
        "Авто-ответ при новом заказе",
        default="Спасибо за заказ! Аккаунт будет выдан в ближайшее время.",
    )
    print()

    content = (
        f"TELEGRAM_BOT_TOKEN={tg_token}\n"
        f"TELEGRAM_ADMIN_ID={tg_admin}\n"
        f"\n"
        f"FUNPAY_GOLDEN_KEY={fp_golden}\n"
        f"FUNPAY_PHPSESSID={fp_phpsess}\n"
        f"FUNPAY_USER_ID={fp_user_id}\n"
        f"FUNPAY_POLL_INTERVAL={poll_interval}\n"
        f"\n"
        f"TELEGRAM_PROXY={tg_proxy}\n"
        f"FUNPAY_PROXY={fp_proxy}\n"
        f"\n"
        f"DATABASE_URL=sqlite+aiosqlite:///./bot.db\n"
        f"AUTO_REPLY_TEXT={auto_reply}\n"
    )

    ENV_PATH.write_text(content, encoding="utf-8")
    print(f"[OK] Конфигурация сохранена: {ENV_PATH}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nПрервано пользователем.")
        sys.exit(1)
