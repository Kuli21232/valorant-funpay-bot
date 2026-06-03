"""Диагностика: какой IP видит Python и как реагирует Riot.

Зачем нужен этот скрипт:
  - Браузер (особенно Яндекс) может использовать встроенный туннель или
    системный прокси, которого Python не видит → IP в браузере ≠ IP Python.
  - Riot Auth возвращает `invalid_request` для запросов из датацентровых
    IP (cloud/VPN/прокси) — независимо от логина/пароля и captcha.
  - Этот скрипт не тратит 2captcha и не требует логин — просто GET/PUT
    и показывает что Riot отвечает.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import aiohttp


def _print_section(title: str) -> None:
    print()
    print("=" * 64)
    print(f"  {title}")
    print("=" * 64)


async def _get_json(sess: aiohttp.ClientSession, url: str) -> dict:
    async with sess.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
        return await r.json(content_type=None)


async def check_my_ip(sess: aiohttp.ClientSession) -> None:
    _print_section("1. Реальный IP вашего Python (НЕ браузера)")
    sources = [
        ("ifconfig.me", "https://ifconfig.me/all.json"),
        ("ipinfo.io",   "https://ipinfo.io/json"),
        ("ipapi.co",    "https://ipapi.co/json/"),
    ]
    for name, url in sources:
        try:
            data = await _get_json(sess, url)
            ip = data.get("ip") or data.get("ip_addr")
            country = data.get("country") or data.get("country_code")
            org = data.get("org") or data.get("asn") or data.get("connection", {}).get("org", "")
            print(f"  {name:14}  IP={ip}  country={country}  org={org!r}")
        except Exception as e:
            print(f"  {name:14}  ERROR: {e}")
    print()
    print("Если IP начинается с 95.* или подобных российских блоков — отлично.")
    print("Если IPv6 (2a0e:, 2a03:, 2600:) или GCP/AWS/Azure — это VPN/прокси,")
    print("Riot их блокирует.")


async def check_riot_response(sess: aiohttp.ClientSession) -> None:
    _print_section("2. Что отвечает Riot Auth на ВАШ IP")

    login_url = (
        "https://authenticate.riotgames.com/?client_id=prod-xsso-riotgames"
        "&code_challenge=t&method=riot_identity&platform=web"
        "&redirect_uri=https%3A%2F%2Fauth.riotgames.com%2F&security_profile=low"
    )

    # GET — должен дать 200 + sitekey + country
    try:
        async with sess.get(login_url) as r:
            html = await r.text()
            country_html = ""
            import re
            m = re.search(r'"country":"(\w+)"', html)
            if m:
                country_html = m.group(1)
            cookies = [c.key for c in sess.cookie_jar]
            print(f"  GET  /authenticate/  → HTTP {r.status}, {len(html)} bytes")
            print(f"    Country в HTML:   {country_html or '— не найдено —'}")
            print(f"    Cookies от Riot:  {cookies}")
    except Exception as e:
        print(f"  GET failed: {e}")
        return

    # PUT — без captcha, но с правильными headers (бесплатный тест)
    try:
        async with sess.put(
            "https://authenticate.riotgames.com/api/v1/login",
            json={"type": "auth", "remember": True, "language": "en_US",
                  "riot_identity": {"username": "test", "password": "test"}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": "https://authenticate.riotgames.com",
                "Referer": login_url,
            },
            allow_redirects=False,
        ) as r:
            body = await r.text()
            print()
            print(f"  PUT  /api/v1/login   → HTTP {r.status}")
            print(f"    Ответ Riot:  {body}")
    except Exception as e:
        print(f"  PUT failed: {e}")
        return

    print()
    print("Возможные исходы:")
    print('  • {"type":"error","error":"invalid_request","country":"rus"}')
    print("     → IP российский, но flow требует captcha. Можем продолжать.")
    print('  • {"type":"error","error":"invalid_request","country":"deu"} или другая страна')
    print("     → Riot видит запрос НЕ из РФ → датацентровый/VPN IP, заблокирован.")
    print('  • Что-то другое — пришлите мне разработчику.')


async def main() -> None:
    print("=" * 64)
    print("  Диагностика подключения к Riot Auth")
    print("=" * 64)
    print()
    print("Скрипт ничего не тратит и не требует логин/пароль.")
    print("Покажет какой IP видит ваш Python и как реагирует Riot.")
    print()
    print("Запустите сначала С ВКЛЮЧЕННЫМ VPN — посмотрите результат.")
    print("Потом ВЫКЛЮЧИТЕ VPN полностью (и системные настройки тоже!)")
    print("и запустите ещё раз — сравните.")
    print()

    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    conn = aiohttp.TCPConnector(ssl=ctx, force_close=False)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    async with aiohttp.ClientSession(
        connector=conn, headers=headers,
        cookie_jar=aiohttp.CookieJar(unsafe=True),
        timeout=aiohttp.ClientTimeout(total=30),
    ) as sess:
        await check_my_ip(sess)
        await check_riot_response(sess)

    print()
    print("=" * 64)


if __name__ == "__main__":
    asyncio.run(main())
