"""hCaptcha solver — supports CapSolver (primary, recommended for Riot)
and 2captcha (fallback).

Riot's auth flow uses invisible hCaptcha Enterprise. Practice shows:
  • 2captcha:   often times out or returns ERROR_CAPTCHA_UNSOLVABLE
  • CapSolver:  specialised in enterprise hCaptcha, 80-95% success
  • anti-captcha: ~50-70% success

The two services share the same API shape (createTask + getTaskResult on
api.{capsolver|2captcha}.com), so one `_PollingClient` class implements both.

We use `force_close=True` on every HTTP call — these services close idle
keep-alive sockets, which causes ServerDisconnectedError on the next poll.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

from config import settings

logger = logging.getLogger(__name__)


class CaptchaError(Exception):
    """Could not solve captcha."""


# ----------------------------------------------------------------------------

class _PollingClient:
    """Shared logic for CapSolver / 2captcha (both have identical JSON shape)."""

    name: str = "captcha"
    create_url: str = ""
    result_url: str = ""
    balance_url: str = ""

    def __init__(self, api_key: str):
        self._key = api_key

    @property
    def enabled(self) -> bool:
        return bool(self._key)

    async def solve_hcaptcha(
        self, sitekey: str, page_url: str,
        invisible: bool = True, timeout_seconds: int = 180,
        rqdata: Optional[str] = None,
        enterprise: bool = True,
    ) -> str:
        """Solve hCaptcha. For Riot we always use Enterprise mode + rqdata if
        we've extracted it from the page — this triples the success rate."""
        if not self._key:
            raise CaptchaError(f"{self.name}: API key not set")
        if not sitekey:
            raise CaptchaError(f"{self.name}: empty sitekey")

        task: dict = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": page_url,
            "websiteKey": sitekey,
            "isInvisible": bool(invisible),
            "userAgent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        if enterprise:
            # 2captcha + CapSolver both accept enterprisePayload to enable
            # Enterprise solver. rqdata, if we have it, goes inside.
            ep: dict = {"sentry": True}
            if rqdata:
                ep["rqdata"] = rqdata
            task["enterprisePayload"] = ep
            # Some providers additionally honor `isEnterprise: true`
            task["isEnterprise"] = True
        task_id = await self._post_json(
            self.create_url, {"clientKey": self._key, "task": task},
            expected_field="taskId",
        )
        logger.info("[%s] task created id=%s (enterprise=%s, rqdata=%s) — waiting",
                    self.name, task_id, enterprise, bool(rqdata))
        return await self._wait_for_result(int(task_id), timeout_seconds)

    async def get_balance(self) -> float:
        if not self._key:
            raise CaptchaError(f"{self.name}: API key not set")
        data = await self._post_json(self.balance_url, {"clientKey": self._key})
        return float(data.get("balance", 0.0))

    async def _post_json(self, url: str, payload: dict,
                         expected_field: str = "") -> object:
        connector = aiohttp.TCPConnector(force_close=True)
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as sess:
            async with sess.post(url, json=payload) as r:
                data = await r.json(content_type=None)
        if data.get("errorId"):
            raise CaptchaError(
                f"{self.name} error code={data.get('errorCode')!r} "
                f"desc={data.get('errorDescription')!r}"
            )
        if expected_field:
            val = data.get(expected_field)
            if val is None:
                raise CaptchaError(
                    f"{self.name}: no {expected_field} in response: {data}"
                )
            return val
        return data

    async def _wait_for_result(self, task_id: int, timeout_seconds: int) -> str:
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        poll_count = 0
        while asyncio.get_event_loop().time() < deadline:
            poll_count += 1
            try:
                data = await self._post_json(
                    self.result_url,
                    {"clientKey": self._key, "taskId": task_id},
                )
            except CaptchaError:
                raise
            except Exception as e:
                logger.warning("[%s] poll #%d transient: %s — retrying",
                               self.name, poll_count, e)
                await asyncio.sleep(5)
                continue
            status = data.get("status")
            if status == "ready":
                sol = data.get("solution", {})
                token = sol.get("gRecaptchaResponse") or sol.get("token")
                if not token:
                    raise CaptchaError(f"{self.name}: no token in solution: {sol}")
                logger.info("[%s] solved task=%d (%d chars)",
                            self.name, task_id, len(token))
                return token
            if poll_count % 6 == 0:
                logger.info("[%s] still processing task=%d (~%ds)",
                            self.name, task_id, poll_count * 5 + 5)
            await asyncio.sleep(5)
        raise CaptchaError(
            f"{self.name}: solve timeout after {timeout_seconds}s "
            f"(task_id={task_id})"
        )


# ----------------------------------------------------------------------------

class CapSolverClient(_PollingClient):
    name = "capsolver"
    create_url = "https://api.capsolver.com/createTask"
    result_url = "https://api.capsolver.com/getTaskResult"
    balance_url = "https://api.capsolver.com/getBalance"


class TwoCaptchaClient(_PollingClient):
    name = "2captcha"
    create_url = "https://api.2captcha.com/createTask"
    result_url = "https://api.2captcha.com/getTaskResult"
    balance_url = "https://api.2captcha.com/getBalance"


class RuCaptchaClient(_PollingClient):
    name = "rucaptcha"
    create_url = "https://api.rucaptcha.com/createTask"
    result_url = "https://api.rucaptcha.com/getTaskResult"
    balance_url = "https://api.rucaptcha.com/getBalance"


# ----------------------------------------------------------------------------

class CaptchaSolver:
    """Top-level entry. Uses CapSolver if configured, falls back to 2captcha / RuCaptcha.
    Order matters: CapSolver tried first because it actually solves Riot's
    enterprise hCaptcha."""

    def __init__(self):
        self._providers: list[_PollingClient] = []
        if settings.CAPSOLVER_KEY:
            self._providers.append(CapSolverClient(settings.CAPSOLVER_KEY.strip()))
        if settings.TWOCAPTCHA_KEY:
            self._providers.append(TwoCaptchaClient(settings.TWOCAPTCHA_KEY.strip()))
        if settings.RUCAPTCHA_KEY:
            self._providers.append(RuCaptchaClient(settings.RUCAPTCHA_KEY.strip()))

    @property
    def enabled(self) -> bool:
        return any(p.enabled for p in self._providers)

    @property
    def provider_names(self) -> list[str]:
        return [p.name for p in self._providers]

    async def solve_hcaptcha(self, sitekey: str, page_url: str,
                              invisible: bool = True,
                              timeout_seconds: int = 180,
                              rqdata: Optional[str] = None) -> str:
        if not self._providers:
            raise CaptchaError(
                "No captcha provider configured. Set CAPSOLVER_KEY in .env "
                "(recommended) — register at capsolver.com, top up $3+."
            )
        last_err: Optional[Exception] = None
        for prov in self._providers:
            try:
                return await prov.solve_hcaptcha(
                    sitekey, page_url, invisible=invisible,
                    timeout_seconds=timeout_seconds,
                    rqdata=rqdata,
                )
            except CaptchaError as e:
                logger.warning("[%s] failed: %s — trying next provider", prov.name, e)
                last_err = e
        raise CaptchaError(f"All providers failed. Last error: {last_err}")

    async def get_balances(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for prov in self._providers:
            try:
                out[prov.name] = await prov.get_balance()
            except Exception as e:
                logger.warning("[%s] balance check failed: %s", prov.name, e)
                out[prov.name] = -1.0
        return out
