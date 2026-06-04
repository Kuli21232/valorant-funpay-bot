"""Riot Sign-On authentication via the modern flow.

Flow (mirrors Riot Mobile + the web Riot Account site):
  1. GET https://authenticate.riotgames.com/?client_id=prod-xsso-riotgames
     &code_challenge=<PKCE>&method=riot_identity&platform=web
     &redirect_uri=<auth.riotgames.com/authorize?...>
     &security_profile=low
     → returns HTML containing the hCaptcha sitekey
     → sets authenticator.sid cookie

  2. Solve invisible hCaptcha via 2captcha (~$0.003, ~20 sec)

  3. PUT https://authenticate.riotgames.com/api/v1/login
     body: {"type":"auth","remember":true,"language":"en_US",
            "riot_identity":{"username":..., "password":...,
                             "captcha":"hcaptcha <token>"}}
     → returns {"type":"response","response":{"mode":"redirect",
                "parameters":{"uri":"https://auth.riotgames.com/authorize?...&code=..."}}}
     OR {"type":"multifactor", ...}
     OR {"type":"auth","error":"auth_failure"} (real wrong credentials)

  4. Follow the redirect URI to auth.riotgames.com/authorize?code=...
     → 302 to xsso.riotgames.com/redirect with code+state
     → Riot sets RSO cookies (access_token, id_token, ssid, tdid)

  5. Exchange access_token for entitlement, puuid, region (same as before)

This flow is the one Riot uses today (verified by capturing a real browser
login). The previous endpoint auth.riotgames.com/api/v1/authorization still
exists but is being phased out and refuses logins without captcha.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import secrets
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import quote, urlencode

import aiohttp
from aiohttp_socks import ProxyConnector

from config import settings
from riot.captcha import CaptchaError, CaptchaSolver

logger = logging.getLogger(__name__)


_RIOT_UA_BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Real Riot hCaptcha sitekey (used by authenticate.riotgames.com).
# Verified from public open-source projects and live captured traffic.
# Override via RIOT_HCAPTCHA_SITEKEY in .env if Riot changes it.
_KNOWN_RIOT_SITEKEYS = [
    "b1c1d2c7-8b6e-4824-8d2b-d3f6df0e7e2d",
    "ad6f7f47-6db5-4b8c-93e8-5a37f4b9c6dd",
    "019f1553-3845-481c-a6f5-5a60ccf6d830",
]

# Test sitekeys we should never use as fallback (2captcha returns a fake 36-char
# token for these — Riot rejects it as invalid_request).
_TEST_SITEKEYS = {
    "10000000-ffff-ffff-ffff-000000000001",
    "00000000-0000-0000-0000-000000000000",
}


# ----- exceptions ------------------------------------------------------------

class AuthError(Exception): ...
class InvalidCredentials(AuthError): ...
class RateLimited(AuthError): ...
class CaptchaRequired(AuthError): ...
class TokensExpired(AuthError): ...


class MfaRequired(AuthError):
    def __init__(self, email_hint: str):
        super().__init__(f"MFA required, code sent to {email_hint}")
        self.email_hint = email_hint


# ----- tokens dataclass ------------------------------------------------------

@dataclass
class RiotTokens:
    access_token: str
    id_token: str
    entitlement_token: str
    puuid: str
    region: str
    expires_at: datetime
    ssid: str
    sub: str = ""

    @property
    def is_expired(self) -> bool:
        return datetime.utcnow() >= self.expires_at - timedelta(minutes=2)


# ----- PKCE helpers ----------------------------------------------------------

def _make_pkce() -> tuple[str, str]:
    """Generate code_verifier + code_challenge (S256)."""
    code_verifier = secrets.token_urlsafe(64)[:64]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return code_verifier, challenge


def _make_state() -> str:
    return secrets.token_hex(20)


def _build_login_url(code_challenge: str, state: str) -> str:
    """Build the URL the user's browser navigates to before login.
    This sets up the OAuth/PKCE session on the server side."""
    inner_redirect = (
        "https://auth.riotgames.com/authorize"
        "?client_id=prod-xsso-riotgames"
        f"&code_challenge={code_challenge}"
        "&code_challenge_method=S256"
        "&redirect_uri=" + quote("https://xsso.riotgames.com/redirect", safe="")
        + "&response_type=code"
        "&scope=" + quote("openid account email offline_access", safe="")
        + f"&state={state}"
    )
    return (
        "https://authenticate.riotgames.com/"
        "?client_id=prod-xsso-riotgames"
        f"&code_challenge={code_challenge}"
        "&method=riot_identity"
        "&platform=web"
        "&redirect_uri=" + quote(inner_redirect, safe="")
        + "&security_profile=low"
    )


# ----- SSL --------------------------------------------------------------------

def _make_ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    ctx.set_alpn_protocols(["http/1.1"])
    return ctx


def _make_session(proxy: Optional[str] = None) -> aiohttp.ClientSession:
    """Create aiohttp session. For SOCKS5/SOCKS4 proxies uses ProxyConnector
    (aiohttp-socks), because aiohttp's built-in proxy support only handles HTTP.
    For HTTP proxies the connector is a plain TCPConnector and the proxy URL is
    passed per-request as usual."""
    ssl_ctx = _make_ssl_context()
    if proxy and proxy.lower().startswith(
        ("socks5://", "socks4://", "socks4a://", "http://", "https://")
    ):
        # ProxyConnector handles BOTH SOCKS and HTTP, and correctly url-decodes
        # username/password from the proxy URL (aiohttp's per-request proxy=
        # mishandles creds containing special chars like '-' or '@').
        connector = ProxyConnector.from_url(proxy, ssl=ssl_ctx, force_close=False)
        _session_proxy = None
    else:
        connector = aiohttp.TCPConnector(ssl=ssl_ctx, force_close=False)
        _session_proxy = proxy  # no proxy at all

    session = aiohttp.ClientSession(
        connector=connector,
        headers={
            "User-Agent": _RIOT_UA_BROWSER,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        },
        timeout=aiohttp.ClientTimeout(total=120),
        cookie_jar=aiohttp.CookieJar(unsafe=True),
    )
    # Attach resolved per-request proxy (None for SOCKS — connector handles it)
    session._rso_per_request_proxy = _session_proxy
    return session


# ----- helpers ----------------------------------------------------------------

_SITEKEY_PATTERNS = [
    re.compile(r'data-sitekey=["\']([0-9a-fA-F\-]{20,})["\']'),
    re.compile(r'"sitekey"\s*:\s*"([0-9a-fA-F\-]{20,})"'),
    re.compile(r"sitekey['\"]?\s*[:=]\s*['\"]([0-9a-fA-F\-]{20,})['\"]"),
    # JSON forms common in modern SPAs
    re.compile(r'"hcaptcha_sitekey"\s*:\s*"([0-9a-fA-F\-]{20,})"'),
    re.compile(r'"key"\s*:\s*"([0-9a-fA-F\-]{20,})"'),
    # Generic UUID match anywhere (last resort — picks up any UUID in body)
    re.compile(r'\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
               r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b'),
]


def _extract_sitekey(html: str) -> Optional[str]:
    """Find the hCaptcha sitekey UUID inside the authenticate page HTML/JSON."""
    seen: list[str] = []
    for pat in _SITEKEY_PATTERNS:
        for m in pat.finditer(html):
            candidate = m.group(1).lower()
            if candidate in _TEST_SITEKEYS:
                continue
            if candidate not in seen:
                seen.append(candidate)
    # Prefer a sitekey we know belongs to Riot
    for known in _KNOWN_RIOT_SITEKEYS:
        if known in seen:
            return known
    # Otherwise take the first non-test UUID we saw
    return seen[0] if seen else None


def _resolve_sitekey(html: str) -> str:
    """Pick the hCaptcha sitekey to use. Priority:
    1) RIOT_HCAPTCHA_SITEKEY from .env
    2) Sitekey extracted from the page HTML
    3) The first known-Riot sitekey (as a documented constant)
    """
    override = (settings.RIOT_HCAPTCHA_SITEKEY or "").strip().lower()
    if override and override not in _TEST_SITEKEYS:
        logger.info("[RSO] using sitekey from .env: %s", override)
        return override
    found = _extract_sitekey(html)
    if found:
        return found
    # Last resort: a known Riot sitekey constant. Better than the hCaptcha
    # test sitekey (which always fails) — Riot may still reject if they
    # rotated it, but at least 2captcha returns a real token.
    logger.warning("[RSO] sitekey not found in HTML — using known Riot constant. "
                   "If login fails, capture sitekey from your browser and set "
                   "RIOT_HCAPTCHA_SITEKEY in .env.")
    return _KNOWN_RIOT_SITEKEYS[0]


_CODE_RE = re.compile(r"[?&]code=([^&#]+)")


def _extract_code(uri: str) -> Optional[str]:
    m = _CODE_RE.search(uri)
    return m.group(1) if m else None


def _redact_proxy(proxy: str) -> str:
    """Hide password in proxy URL for safe logging."""
    return re.sub(r"://([^:]+):([^@]+)@", r"://\1:***@", proxy)


# ----- main class -------------------------------------------------------------

class RsoAuth:
    LOGIN_API = "https://authenticate.riotgames.com/api/v1/login"
    AUTHORIZE_URL = "https://auth.riotgames.com/authorize"  # exchange code → tokens
    ENTITLEMENT_URL = "https://entitlements.auth.riotgames.com/api/token/v1"
    USERINFO_URL = "https://auth.riotgames.com/userinfo"
    REGION_URL = "https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant"

    def __init__(self):
        self._captcha = CaptchaSolver()

    async def authenticate(
        self,
        username: str,
        password: str,
        mfa_code: Optional[str] = None,
        proxy: Optional[str] = None,
    ) -> RiotTokens:
        if not self._captcha.enabled:
            raise CaptchaError(
                "No captcha provider configured. Set CAPSOLVER_KEY in .env "
                "(recommended for Riot — register at capsolver.com, $3+)."
            )
        logger.info("[RSO] captcha providers: %s", self._captcha.provider_names)

        # Use configured Riot proxy if caller didn't pass one
        proxy = proxy or (settings.RIOT_PROXY or None)
        if proxy:
            logger.info("[RSO] using proxy: %s", _redact_proxy(proxy))

        code_verifier, code_challenge = _make_pkce()
        state = _make_state()
        login_url = _build_login_url(code_challenge, state)

        # Retry session creation up to 5x — residential rotating proxies
        # often return 407/502 on first connection while waiting for an
        # exit-node to be assigned. Each new session = new connector =
        # new chance at a healthy exit-node.
        last_proxy_err: Optional[Exception] = None
        sess = None
        for attempt in range(1, 6):
            sess = _make_session(proxy)
            _p = getattr(sess, "_rso_per_request_proxy", None)
            try:
                # quick probe — if proxy is bad we'll find out here
                async with sess.get(login_url, proxy=_p, allow_redirects=False) as probe:
                    if probe.status >= 500 or probe.status == 407:
                        raise aiohttp.ClientError(f"proxy returned HTTP {probe.status}")
                    # success — re-do the request below for full flow
                    break
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as e:
                last_proxy_err = e
                logger.warning("[RSO] proxy attempt %d/5 failed: %s — retrying",
                               attempt, str(e)[:120])
                await sess.close()
                sess = None
                await asyncio.sleep(1.5)
        if sess is None:
            raise AuthError(f"Proxy unreachable after 5 attempts: {last_proxy_err}")

        async with sess:
            _p = getattr(sess, "_rso_per_request_proxy", None)
            # Step 1: load the authenticate page to set authenticator.sid cookie
            #         and discover the hCaptcha sitekey from HTML.
            # We start with allow_redirects=False so we can see exactly what
            # Riot returns (302 vs 200) and preserve all Set-Cookie headers.
            async with sess.get(login_url, proxy=_p, allow_redirects=False) as r:
                html = await r.text()
                set_cookie_hdrs = r.headers.getall("Set-Cookie", [])
                logger.info("[RSO] GET authenticate.* → HTTP %s, %d bytes, Set-Cookie x%d",
                            r.status, len(html), len(set_cookie_hdrs))

                # If Riot sends a redirect, follow it manually once so the
                # cookie jar still picks up any intermediate cookies.
                if 300 <= r.status < 400:
                    location = r.headers.get("Location", "")
                    logger.info("[RSO] following redirect → %s", location[:200])
                    async with sess.get(location, proxy=_p, allow_redirects=True) as r2:
                        html = await r2.text()
                        logger.info("[RSO] after redirect → HTTP %s, %d bytes",
                                    r2.status, len(html))

            cookies_after_get = {c.key: c.value for c in sess.cookie_jar}
            logger.info("[RSO] cookies after GET: %s",
                        sorted(cookies_after_get.keys()))

            sitekey = _resolve_sitekey(html)
            logger.info("[RSO] hCaptcha sitekey from HTML: %s", sitekey)
            # Dump the page on first failure so we can refine the extractor
            try:
                from pathlib import Path
                Path("riot_authenticate_debug.html").write_text(html, encoding="utf-8")
            except Exception:
                pass

            # Try to grab rqdata via headless Playwright — captcha solvers
            # succeed 2-3× more often when rqdata is provided.
            rqdata: Optional[str] = None
            if not settings.RIOT_SKIP_RQDATA:
                try:
                    from riot.rqdata_grabber import grab_hcaptcha_params
                    logger.info("[RSO] grabbing rqdata via Playwright (5-10s)...")
                    pw_sitekey, rqdata = await grab_hcaptcha_params(login_url)
                    if pw_sitekey and pw_sitekey != sitekey:
                        logger.info("[RSO] Playwright found different sitekey: %s "
                                    "→ using it", pw_sitekey)
                        sitekey = pw_sitekey
                    if rqdata:
                        logger.info("[RSO] rqdata captured (%d chars)", len(rqdata))
                except Exception as e:
                    logger.warning("[RSO] rqdata grab failed: %s — continuing without", e)
            else:
                logger.info("[RSO] rqdata grab skipped (RIOT_SKIP_RQDATA=true)")

            # If the authenticator.sid cookie did NOT come back, the login PUT
            # will be rejected as invalid_request — Riot ties captcha to that
            # session. Warn loudly.
            if "authenticator.sid" not in cookies_after_get:
                logger.error(
                    "[RSO] authenticator.sid cookie NOT received from GET — "
                    "PUT /login is virtually certain to fail. This usually means "
                    "the GET was redirected to a CDN that doesn't set cookies, "
                    "or your IP is geo-blocked from authenticate.riotgames.com."
                )

            # Step 2: solve invisible hCaptcha via captcha provider(s)
            try:
                captcha_token = await self._captcha.solve_hcaptcha(
                    sitekey=sitekey,
                    page_url=login_url,
                    invisible=True,
                    rqdata=rqdata,
                )
            except CaptchaError as e:
                raise CaptchaRequired(f"captcha provider failed: {e}")

            # Step 3: PUT credentials with captcha
            body = {
                "type": "auth",
                "remember": True,
                "language": "en_US",
                "riot_identity": {
                    "username": username,
                    "password": password,
                    "captcha": f"hcaptcha {captcha_token}",
                },
            }
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": "https://authenticate.riotgames.com",
                "Referer": login_url,
            }
            cookies_before_put = {c.key: c.value for c in sess.cookie_jar}
            logger.info("[RSO] cookies sent with PUT: %s",
                        sorted(cookies_before_put.keys()))

            async def _do_put(captcha_token_to_use: str) -> dict:
                payload = {
                    **body,
                    "riot_identity": {
                        **body["riot_identity"],
                        "captcha": f"hcaptcha {captcha_token_to_use}",
                    },
                }
                async with sess.put(self.LOGIN_API, json=payload, headers=headers,
                                    proxy=_p, allow_redirects=False) as r:
                    logger.info("[RSO] PUT /api/v1/login → HTTP %s", r.status)
                    resp = await r.json(content_type=None)
                    raw_txt = await r.text() if logger.isEnabledFor(logging.WARNING) else ""
                logger.info("[RSO] login response type=%s err=%s",
                            resp.get("type"), resp.get("error"))
                return resp, raw_txt

            data, raw = await _do_put(captcha_token)

            # Handle MFA branch
            if data.get("type") == "multifactor":
                if not mfa_code:
                    mf = data.get("multifactor") or {}
                    hint = mf.get("email") or mf.get("emailHint") or "your email"
                    raise MfaRequired(hint)
                async with sess.put(
                    self.LOGIN_API,
                    json={"type": "multifactor", "code": mfa_code,
                          "rememberDevice": True},
                    headers=headers, proxy=_p, allow_redirects=False,
                ) as r:
                    logger.info("[RSO] MFA PUT → HTTP %s", r.status)
                    data = await r.json(content_type=None)
                    raw = await r.text() if logger.isEnabledFor(logging.WARNING) else ""

            # If Riot returns invalid_request WITH a captcha challenge,
            # we must solve THAT captcha (using its rqdata) and retry.
            if data.get("error") == "invalid_request" and "captcha" in data:
                cap_info = data["captcha"].get("hcaptcha") or {}
                challenge_sitekey = cap_info.get("key") or sitekey
                challenge_rqdata = cap_info.get("data")
                logger.warning(
                    "[RSO] Riot demanded a captcha challenge (country=%s, sitekey=%s, rqdata=%s) — solving...",
                    data.get("country", "?"),
                    challenge_sitekey,
                    "yes" if challenge_rqdata else "no",
                )
                try:
                    new_token = await self._captcha.solve_hcaptcha(
                        sitekey=challenge_sitekey,
                        page_url=login_url,
                        invisible=True,
                        rqdata=challenge_rqdata,
                    )
                except CaptchaError as e:
                    raise CaptchaRequired(f"challenge captcha failed: {e}")
                data, raw = await _do_put(new_token)

            # Fallback: if the new authenticate endpoint rejects with
            # invalid_request WITHOUT a captcha challenge, try the legacy
            # auth.riotgames.com/api/v1/authorization endpoint.
            if data.get("error") == "invalid_request":
                logger.warning(
                    "[RSO] new endpoint returned invalid_request (country=%s) — "
                    "trying legacy auth.riotgames.com/api/v1/authorization",
                    data.get("country", "?"),
                )
                legacy_body = {
                    "type": "auth",
                    "username": username,
                    "password": password,
                    "remember": True,
                    "language": "en_US",
                    "captcha": f"hcaptcha {captcha_token}",
                }
                legacy_headers = {
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                }
                async with sess.post(
                    "https://auth.riotgames.com/api/v1/authorization",
                    json=legacy_body,
                    headers=legacy_headers,
                    proxy=_p,
                    allow_redirects=False,
                ) as r2:
                    logger.info("[RSO] legacy POST → HTTP %s", r2.status)
                    data = await r2.json(content_type=None)
                    raw = await r2.text() if logger.isEnabledFor(logging.WARNING) else ""
                logger.info("[RSO] legacy response type=%s err=%s",
                            data.get("type"), data.get("error"))

            self._raise_on_error(data, username, raw=raw)

            # Step 4: follow redirect, capture access_token from RSO cookies
            redirect_uri = data["response"]["parameters"]["uri"]
            logger.info("[RSO] redirect target: %s", redirect_uri[:120])

            # Visit the auth.riotgames.com/authorize?...&code=... URL.
            # Riot will set __Secure-access_token, ssid, etc. as cookies.
            async with sess.get(redirect_uri, proxy=_p, allow_redirects=True) as r:
                logger.info("[RSO] follow redirect → HTTP %s, final URL=%s",
                            r.status, str(r.url)[:120])

            access_token = self._get_cookie(sess, "__Secure-access_token")
            id_token = self._get_cookie(sess, "__Secure-id_token")
            ssid = self._get_cookie(sess, "ssid") or ""

            if not access_token or not id_token:
                raise AuthError(
                    f"Did not receive RSO tokens after redirect. "
                    f"cookies={list(self._cookie_names(sess))}"
                )

            # Compute expires_at from the access_token JWT exp claim (or default 1h)
            expires_at = self._decode_jwt_exp(access_token) or \
                         datetime.utcnow() + timedelta(hours=1)

            # Step 5: fetch entitlement, puuid, region
            entitlement = await self._fetch_entitlement(sess, access_token, _p)
            puuid = await self._fetch_puuid(sess, access_token, _p)
            region = await self._fetch_region(sess, access_token, id_token, _p)

            tokens = RiotTokens(
                access_token=access_token,
                id_token=id_token,
                entitlement_token=entitlement,
                puuid=puuid,
                region=region,
                expires_at=expires_at,
                ssid=ssid,
                sub=puuid,
            )
            logger.info("RSO auth OK: %s (region=%s, puuid=%s...)",
                        username, region, puuid[:8])
            return tokens

    async def refresh(self, ssid: str, proxy: Optional[str] = None) -> RiotTokens:
        """Try to refresh tokens using the stored ssid cookie.

        ssid is set on .riotgames.com and lasts ~1 year. With it we can hit
        the authorize endpoint and Riot mints fresh access_token/id_token
        without re-asking for credentials or captcha.
        """
        if not ssid:
            raise TokensExpired("No ssid stored")

        proxy = proxy or (settings.RIOT_PROXY or None)

        code_verifier, code_challenge = _make_pkce()
        state = _make_state()
        authorize_url = (
            "https://auth.riotgames.com/authorize"
            "?client_id=prod-xsso-riotgames"
            f"&code_challenge={code_challenge}&code_challenge_method=S256"
            "&redirect_uri=" + quote("https://xsso.riotgames.com/redirect", safe="")
            + "&response_type=code"
            "&scope=" + quote("openid account email offline_access", safe="")
            + f"&state={state}"
        )

        async with _make_session(proxy) as sess:
            _p = getattr(sess, "_rso_per_request_proxy", None)
            sess.cookie_jar.update_cookies(
                {"ssid": ssid},
                response_url=aiohttp.helpers.URL("https://auth.riotgames.com"),
            )
            async with sess.get(authorize_url, proxy=_p, allow_redirects=True) as r:
                final = str(r.url)
                logger.info("[RSO] refresh authorize → HTTP %s, final=%s",
                            r.status, final[:120])

            access_token = self._get_cookie(sess, "__Secure-access_token")
            id_token = self._get_cookie(sess, "__Secure-id_token")
            new_ssid = self._get_cookie(sess, "ssid") or ssid

            if not access_token or not id_token:
                raise TokensExpired("ssid did not produce fresh tokens")

            expires_at = self._decode_jwt_exp(access_token) or \
                         datetime.utcnow() + timedelta(hours=1)
            entitlement = await self._fetch_entitlement(sess, access_token, _p)
            puuid = await self._fetch_puuid(sess, access_token, _p)
            region = await self._fetch_region(sess, access_token, id_token, _p)

            tokens = RiotTokens(
                access_token=access_token,
                id_token=id_token,
                entitlement_token=entitlement,
                puuid=puuid,
                region=region,
                expires_at=expires_at,
                ssid=new_ssid,
                sub=puuid,
            )
            logger.info("RSO refresh OK: puuid=%s... region=%s",
                        puuid[:8], region)
            return tokens

    # ----- HTTP helpers -----

    async def _fetch_entitlement(self, sess, access_token: str, proxy) -> str:
        async with sess.post(
            self.ENTITLEMENT_URL, json={},
            headers={"Authorization": f"Bearer {access_token}"},
            proxy=proxy,
        ) as r:
            if r.status != 200:
                text = await r.text()
                raise AuthError(f"entitlement returned {r.status}: {text[:200]}")
            data = await r.json()
        return data["entitlements_token"]

    async def _fetch_puuid(self, sess, access_token: str, proxy) -> str:
        async with sess.get(
            self.USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            proxy=proxy,
        ) as r:
            if r.status != 200:
                text = await r.text()
                raise AuthError(f"userinfo returned {r.status}: {text[:200]}")
            data = await r.json()
        return data["sub"]

    async def _fetch_region(self, sess, access_token: str, id_token: str, proxy) -> str:
        async with sess.put(
            self.REGION_URL, json={"id_token": id_token},
            headers={"Authorization": f"Bearer {access_token}"},
            proxy=proxy,
        ) as r:
            if r.status != 200:
                text = await r.text()
                raise AuthError(f"region returned {r.status}: {text[:200]}")
            data = await r.json()
        return data.get("affinities", {}).get("live") or data.get("token", "eu")

    @staticmethod
    def _get_cookie(sess: aiohttp.ClientSession, name: str) -> Optional[str]:
        for c in sess.cookie_jar:
            if c.key == name:
                return c.value
        return None

    @staticmethod
    def _cookie_names(sess: aiohttp.ClientSession):
        return {c.key for c in sess.cookie_jar}

    @staticmethod
    def _decode_jwt_exp(jwt: str) -> Optional[datetime]:
        """Decode the exp claim from a JWT (no signature verification)."""
        try:
            parts = jwt.split(".")
            if len(parts) < 2:
                return None
            payload = parts[1] + "=" * (-len(parts[1]) % 4)
            decoded = base64.urlsafe_b64decode(payload)
            import json
            data = json.loads(decoded)
            exp = data.get("exp")
            if exp:
                return datetime.utcfromtimestamp(int(exp))
        except Exception:
            pass
        return None

    @staticmethod
    def _raise_on_error(data: dict, username: str = "?", raw: str = "") -> None:
        if data.get("type") == "response":
            return
        err = data.get("error", "")
        logger.warning("[RSO] login NOT OK for %s: type=%s error=%s | raw=%s",
                       username, data.get("type"), err,
                       (raw or "")[:400].replace("\n", " "))
        if err == "auth_failure":
            raise InvalidCredentials("Invalid username or password")
        if err == "rate_limited":
            raise RateLimited("Riot rate-limited authentication")
        if err in ("cloudflare", "captcha_required") or "captcha" in (err or "").lower():
            raise CaptchaRequired(f"Captcha problem: {err}")
        if data.get("type") == "multifactor":
            return
        raise AuthError(f"Unexpected login response: type={data.get('type')} err={err}")
