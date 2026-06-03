import asyncio
import json
import logging
from typing import Any, Optional

import httpx

from config import settings
from funpay.parser import extract_csrf_token

logger = logging.getLogger(__name__)


def _redact(url: str) -> str:
    """Mask credentials in proxy URL for logging."""
    import re
    return re.sub(r"://([^:@]+):([^@]+)@", "://***:***@", url)


class FunPayAuthError(Exception):
    pass


class FunPayClient:
    BASE_URL = "https://funpay.com"

    def __init__(self):
        self._cookies = {
            "golden_key": settings.FUNPAY_GOLDEN_KEY,
            "PHPSESSID": settings.FUNPAY_PHPSESSID,
        }
        self._csrf_token: Optional[str] = None
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self) -> "FunPayClient":
        # IMPORTANT: do NOT put X-Requested-With in default headers — FunPay
        # would return AJAX fragments for every GET. Only set it on /runner/.
        client_kwargs: dict = dict(
            base_url=self.BASE_URL,
            cookies=self._cookies,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,"
                    "image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
            },
            follow_redirects=True,
            timeout=30.0,
        )
        if settings.FUNPAY_PROXY:
            client_kwargs["proxy"] = settings.FUNPAY_PROXY
            logger.info("FunPay using proxy: %s", _redact(settings.FUNPAY_PROXY))
        self._client = httpx.AsyncClient(**client_kwargs)
        await self._fetch_csrf_with_retry()
        return self

    async def _fetch_csrf_with_retry(self, max_attempts: int = 0) -> None:
        """Retry CSRF fetch indefinitely with exponential backoff (cap 60s)."""
        attempt = 0
        delay = 5
        while True:
            attempt += 1
            try:
                await self.fetch_csrf_token()
                return
            except FunPayAuthError:
                # Auth errors mean cookies are bad — don't retry, surface to user
                raise
            except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError,
                    httpx.ConnectTimeout, httpx.ReadTimeout) as e:
                logger.warning(
                    "FunPay connection failed (attempt %d): %s. "
                    "Retrying in %ds... Check your internet/VPN.",
                    attempt, type(e).__name__, delay,
                )
                if max_attempts and attempt >= max_attempts:
                    raise
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()

    async def fetch_csrf_token(self) -> str:
        resp = await self._client.get("/")
        self._check_auth(resp)

        # If we got redirected to login page, cookies are invalid
        final_url = str(resp.url).lower()
        if "login" in final_url or "auth" in final_url:
            raise FunPayAuthError(
                f"Redirected to login page ({final_url}). "
                "Cookies are invalid — update FUNPAY_GOLDEN_KEY and FUNPAY_PHPSESSID in .env"
            )

        token = extract_csrf_token(resp.text)
        if not token:
            # Save HTML for debugging
            try:
                from pathlib import Path
                debug_path = Path("funpay_debug.html")
                debug_path.write_text(resp.text, encoding="utf-8")
                logger.error("Saved response HTML to %s for inspection", debug_path)
            except Exception:
                pass

            # Provide diagnostic hints
            hints = []
            if "Войти" in resp.text or "Sign in" in resp.text or "login" in resp.text.lower()[:5000]:
                hints.append("Page contains login form — cookies likely expired or invalid")
            if len(resp.text) < 1000:
                hints.append(f"Response is suspiciously short ({len(resp.text)} bytes)")
            hint_text = ". ".join(hints) if hints else "Unknown reason"

            raise FunPayAuthError(
                f"Could not extract CSRF token. {hint_text}. "
                f"Check funpay_debug.html and update cookies in .env. "
                f"How to get cookies: browser DevTools (F12) -> Application -> Cookies -> funpay.com -> "
                f"copy 'golden_key' and 'PHPSESSID' values."
            )
        self._csrf_token = token
        logger.info("CSRF token loaded successfully")
        return token

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        resp = await self._client.get(path, **kwargs)
        self._check_auth(resp)
        return resp

    async def post(self, path: str, data: dict, **kwargs: Any) -> httpx.Response:
        headers = {
            "X-Csrf-Token": self._csrf_token or "",
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = await self._client.post(path, data=data, headers=headers, **kwargs)
        self._check_auth(resp)
        return resp

    async def runner_request(self, objects: list[dict]) -> dict:
        payload = {
            "objects": json.dumps(objects),
            "request": "false",
            "csrf_token": self._csrf_token or "",
        }
        headers = {
            "X-Csrf-Token": self._csrf_token or "",
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = await self._client.post("/runner/", data=payload, headers=headers)
        self._check_auth(resp)
        try:
            return resp.json()
        except Exception:
            return {}

    async def send_chat_message(self, chat_id: int, text: str) -> None:
        objects = [
            {
                "action": "chat_message",
                "data": {
                    "node": chat_id,
                    "content": text,
                    "csrf_token": self._csrf_token,
                },
            }
        ]
        await self.runner_request(objects)

    async def fetch_chat_history(self, chat_id: int) -> str:
        resp = await self.get(f"/chat/?node={chat_id}")
        return resp.text

    async def fetch_orders_page(self) -> str:
        resp = await self.get("/orders/trade")
        return resp.text

    async def download_image(self, url: str) -> bytes:
        """Download an image attachment from FunPay chat. Uses the same
        cookied session so private URLs (sfunpay.com/s/...) are accessible."""
        resp = await self._client.get(url)
        self._check_auth(resp)
        if resp.status_code != 200:
            raise IOError(f"image download failed: HTTP {resp.status_code}")
        return resp.content

    def _check_auth(self, resp: httpx.Response) -> None:
        if resp.status_code == 401 or (
            resp.is_redirect and "login" in str(resp.headers.get("location", ""))
        ):
            raise FunPayAuthError("FunPay session expired — update PHPSESSID in .env")
