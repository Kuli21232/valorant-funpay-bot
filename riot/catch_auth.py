"""Поймать реальный запрос логина браузера и попробовать его повторить.

Задача — увидеть какой именно endpoint, headers и body Riot сейчас
принимает, и можем ли мы повторить его из Python (если да — встроим
в основной flow; если нет — это серьёзный анти-бот барьер).
"""
from __future__ import annotations

import asyncio
import json
import re
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

from utils.logging_config import setup_logging
setup_logging()

import aiohttp


def parse_curl(curl: str) -> dict:
    """Извлечь method/url/headers/data из 'Copy as cURL'."""
    curl = curl.strip().replace("\\\n", " ").replace("^\n", " ").replace("`\n", " ")
    # URL
    url_m = re.search(r"curl\s+['\"]([^'\"]+)['\"]", curl) or re.search(r"curl\s+(\S+)", curl)
    url = url_m.group(1) if url_m else ""
    # Method
    method_m = re.search(r"(?:-X|--request)\s+['\"]?([A-Z]+)['\"]?", curl)
    method = method_m.group(1) if method_m else None
    # Headers
    headers = {}
    for hm in re.finditer(r"(?:-H|--header)\s+['\"]([^'\"]+)['\"]", curl):
        line = hm.group(1)
        if ":" in line:
            k, _, v = line.partition(":")
            headers[k.strip()] = v.strip()
    # Cookies via -b
    cm = re.search(r"(?:-b|--cookie)\s+['\"]([^'\"]+)['\"]", curl)
    if cm:
        headers["Cookie"] = cm.group(1)
    # Data
    data = None
    dm = re.search(r"--data-raw\s+['\"](.*?)['\"]\s*(?:--|$)", curl, re.S)
    if not dm:
        dm = re.search(r"--data(?:-binary)?\s+['\"](.*?)['\"]\s*(?:--|$)", curl, re.S)
    if not dm:
        dm = re.search(r"-d\s+['\"](.*?)['\"]\s*(?:--|$)", curl, re.S)
    if dm:
        data = dm.group(1)
    if not method:
        method = "POST" if data else "GET"
    return {"method": method, "url": url, "headers": headers, "data": data}


async def replay(req: dict) -> None:
    print()
    print(f"→ {req['method']} {req['url']}")
    print(f"  Headers: {len(req['headers'])}")
    if req["data"]:
        snippet = req["data"][:200].replace("\n", " ")
        print(f"  Data: {snippet}{'...' if len(req['data']) > 200 else ''}")
    print()

    async with aiohttp.ClientSession() as sess:
        try:
            async with sess.request(
                req["method"], req["url"],
                headers=req["headers"],
                data=req["data"].encode() if req["data"] else None,
                allow_redirects=False,
            ) as r:
                body = await r.text()
                print(f"← Status: {r.status}")
                for k, v in r.headers.items():
                    if k.lower() in ("location", "content-type", "set-cookie", "x-riot-token-type"):
                        print(f"  {k}: {v[:200]}")
                print()
                print(f"← Body ({len(body)} chars):")
                print(body[:1500])
                if len(body) > 1500:
                    print(f"... [+{len(body)-1500} chars]")
                if 200 <= r.status < 400:
                    print()
                    print("[OK] Запрос принят! Сохраню детали для интеграции.")
                    _save_template(req, r.status)
                else:
                    print()
                    print("[!] Запрос отклонён.")
        except Exception as e:
            print(f"[ERROR] {e}")


def _save_template(req: dict, status: int) -> None:
    safe_headers = {}
    for k, v in req["headers"].items():
        kl = k.lower()
        if kl in ("cookie", "authorization"):
            safe_headers[k] = f"<{kl}: {len(v)} chars>"
        else:
            safe_headers[k] = v
    template = {
        "status": status,
        "method": req["method"],
        "url": req["url"],
        "headers_safe": safe_headers,
        "header_names": list(req["headers"].keys()),
        "data_length": len(req["data"]) if req["data"] else 0,
        "data_format": "json" if (req["data"] or "").strip().startswith("{") else "other",
    }
    with open("riot_auth_endpoint.json", "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)
    print()
    print("=" * 64)
    print("[OK] Структура сохранена в riot_auth_endpoint.json")
    print("     Пришлите этот файл разработчику — он встроит точный flow в бота.")
    print("=" * 64)


async def main() -> None:
    print("=" * 64)
    print("  Перехват реального запроса логина Riot")
    print("=" * 64)
    print()
    print("Что нужно сделать:")
    print()
    print("  1. Откройте Chrome (без VPN).")
    print("  2. Перейдите на https://account.riotgames.com")
    print("  3. Нажмите F12 → вкладка Network → галочка 'Preserve log'")
    print("  4. Поставьте фильтр 'Fetch/XHR'")
    print("  5. Нажмите Sign In, введите СВЕЖИЕ логин и пароль, нажмите Sign In")
    print("  6. В Network найдите POST запрос (обычно к authenticate.riotgames.com")
    print("     или auth.riotgames.com) — самый первый, который улетает при клике Sign In")
    print("  7. Правый клик на запросе → Copy → Copy as cURL (bash)")
    print("  8. Вставьте сюда (Ctrl+V), затем пустая строка для завершения")
    print()
    print("Внимание: cURL содержит ваш пароль и cookies — после анализа")
    print("отправляйте только сохранённый riot_auth_endpoint.json (без секретов).")
    print()
    print("=" * 64)
    print("Вставьте cURL команду:")
    print()

    lines = []
    while True:
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            break
        if not line.strip() and lines:
            break
        lines.append(line)

    curl = "\n".join(lines).strip()
    if not curl:
        print("Пусто. Выход.")
        return
    req = parse_curl(curl)
    if not req["url"]:
        print("[!] Не удалось извлечь URL из cURL.")
        return

    print()
    print(f"Распознано:")
    print(f"  Метод: {req['method']}")
    print(f"  URL:   {req['url']}")
    await replay(req)


if __name__ == "__main__":
    asyncio.run(main())
