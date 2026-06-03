"""Diagnostic tool: replay the EXACT 'Sign out everywhere' request.

The most reliable way to terminate Riot sessions via API is to replicate the
exact HTTP request your browser sends when you click 'Sign out everywhere'.

How to capture it:
  1. Open Chrome, log in at https://account.riotgames.com/security
  2. Press F12 → Network tab → check 'Preserve log'
  3. Click 'Sign out everywhere' (or 'Sign out of all other sessions')
  4. In Network, find the request that fired (look for DELETE/POST to
     account.riotgames.com or auth.riotgames.com)
  5. Right-click it → Copy → Copy as cURL (bash)
  6. Run this tool (test_api.bat) and paste the cURL command

This tool parses the cURL, replays it, and — if it works — saves the endpoint
template so the bot can reproduce it for every kick using fresh cookies.
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

import httpx

from utils.logging_config import setup_logging

setup_logging()


def parse_curl(curl: str) -> dict:
    """Extract method, url, headers, data from a 'Copy as cURL' string."""
    curl = curl.strip()
    # Normalize line continuations
    curl = curl.replace("\\\n", " ").replace("^\n", " ").replace("`\n", " ")

    # URL: first quoted string after 'curl'
    url_m = re.search(r"curl\s+['\"]([^'\"]+)['\"]", curl)
    if not url_m:
        url_m = re.search(r"curl\s+(\S+)", curl)
    url = url_m.group(1) if url_m else ""

    # Method
    method_m = re.search(r"(?:-X|--request)\s+([A-Z]+)", curl)
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
    dm = re.search(r"(?:--data-raw|--data|-d)\s+['\"](.*?)['\"]\s*(?:--|$)", curl, re.S)
    if dm:
        data = dm.group(1)

    if not method:
        method = "POST" if data else "GET"

    return {"method": method, "url": url, "headers": headers, "data": data}


async def replay(req: dict) -> None:
    print(f"\n→ {req['method']} {req['url']}")
    async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
        try:
            r = await client.request(
                req["method"], req["url"],
                headers=req["headers"],
                content=req["data"].encode() if req["data"] else None,
            )
            print(f"← Status: {r.status_code}")
            print(f"← Body: {(r.text or '')[:500]}")
            if 200 <= r.status_code < 300:
                print("\n[OK] Запрос успешен! Это рабочий endpoint.")
                _save_template(req)
            else:
                print("\n[!] Запрос вернул не-2xx. Возможно cookies/токен устарел —")
                print("    скопируйте свежий cURL сразу после клика в браузере.")
        except Exception as e:
            print(f"[ERROR] {e}")


def _save_template(req: dict) -> None:
    """Save the FULL working request so it can be integrated/reproduced."""
    # Strip the actual cookie/auth values but keep structure for analysis
    safe_headers = {}
    for k, v in req["headers"].items():
        kl = k.lower()
        if kl in ("cookie", "authorization"):
            safe_headers[k] = f"<{kl}: {len(v)} chars>"
        else:
            safe_headers[k] = v
    template = {
        "method": req["method"],
        "url": req["url"],
        "headers": safe_headers,
        "has_auth_bearer": "authorization" in {h.lower() for h in req["headers"]},
        "data": req["data"],
    }
    with open("riot_logout_endpoint.json", "w", encoding="utf-8") as f:
        json.dump(template, f, ensure_ascii=False, indent=2)
    print("\n" + "=" * 64)
    print("[OK] Запрос сохранён в riot_logout_endpoint.json")
    print("     Пришлите содержимое этого файла разработчику для интеграции.")
    print("=" * 64)


async def main() -> None:
    print("=" * 64)
    print("  Диагностика API логаута Riot")
    print("=" * 64)
    print(__doc__)
    print("=" * 64)
    print("Вставьте cURL команду (целиком), затем Enter и пустую строку:")
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

    print(f"\nРаспознано:")
    print(f"  Метод:  {req['method']}")
    print(f"  URL:    {req['url']}")
    print(f"  Заголовков: {len(req['headers'])}")
    await replay(req)


if __name__ == "__main__":
    asyncio.run(main())
