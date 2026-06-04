"""Browser-driven Riot login via Playwright.

The API flow (manual POST /api/v1/login) is brittle: Riot's authenticator
keeps invalidating our hand-built OAuth URL with `prompt.error: invalid_request`
in the page config, even when the URL parameters match what the real Riot
website builds.

This module does login the human way:
  1. Open https://www.riotgames.com → click "Sign In" (Riot builds its own
     valid OAuth URL, no guesswork on client_id/redirect_uri/security_profile).
  2. Fill the form with username/password.
  3. If the page asks for hCaptcha, extract sitekey + rqdata from the live DOM
     (where the hCaptcha JS has already attached enterprise tokens), solve via
     the captcha provider (CapSolver/2captcha/RuCaptcha), inject the token into
     the hidden response field, submit.
  4. Riot redirects through auth.riotgames.com and writes __Secure-access_token,
     __Secure-id_token, ssid into the cookie jar. We read them straight from
     the browser context.
  5. Re-use the existing aiohttp helpers from RsoAuth to fetch puuid, region,
     entitlement — those endpoints accept the access_token directly and don't
     need a browser.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

from config import settings
from riot.captcha import CaptchaError, CaptchaSolver

logger = logging.getLogger(__name__)


# Re-use the existing dataclass and exceptions so this module is a drop-in
# replacement for RsoAuth.authenticate().
def _imports():
    """Late-import to avoid circular dependency at module load time."""
    from riot.rso_auth import (
        AuthError, CaptchaRequired, InvalidCredentials, MfaRequired,
        RateLimited, RiotTokens, RsoAuth, _make_session,
    )
    return (AuthError, CaptchaRequired, InvalidCredentials, MfaRequired,
            RateLimited, RiotTokens, RsoAuth, _make_session)


# ----- proxy helper ----------------------------------------------------------

def _playwright_proxy_cfg() -> Optional[dict]:
    raw = (settings.RIOT_PROXY or "").strip()
    if not raw:
        return None
    u = urlparse(raw)
    if not u.hostname or not u.port:
        return None
    cfg = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        cfg["username"] = u.username
    if u.password:
        cfg["password"] = u.password
    return cfg


# ----- main class ------------------------------------------------------------

class BrowserAuth:
    # Sign-in entry points we try in order.
    #   account.riotgames.com — Riot's account dashboard. ALWAYS requires
    #     login, so Riot immediately redirects (302) to authenticate.*
    #     with a freshly-minted, valid OAuth URL. This is the most reliable
    #     entry — no Sign-In button hunting required.
    #   riotgames.com / playvalorant.com — fallback in case account.* is
    #     somehow unreachable through the proxy.
    ENTRY_URLS = [
        "https://account.riotgames.com/",
        "https://www.riotgames.com/en",
        "https://playvalorant.com/en-us/",
    ]

    # Selectors for "Sign In" links/buttons across Riot sites (text varies by
    # locale, so we use a fuzzy regex).
    SIGN_IN_SELECTOR = (
        'a:has-text("Sign in"), a:has-text("ВОЙТИ"), a:has-text("Войти"), '
        'button:has-text("Sign in"), button:has-text("ВОЙТИ"), '
        'button:has-text("Войти"), a[href*="auth"], a[href*="login"]'
    )

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    STEALTH_INIT = """
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
    """

    def __init__(self):
        self._captcha = CaptchaSolver()

    async def authenticate(
        self,
        username: str,
        password: str,
        mfa_code: Optional[str] = None,
        proxy: Optional[str] = None,
    ):
        (AuthError, CaptchaRequired, InvalidCredentials, MfaRequired,
         RateLimited, RiotTokens, RsoAuth, _make_session) = _imports()

        # Prefer patchright (Playwright fork with anti-detection patches —
        # passes Cloudflare/Riot bot checks where vanilla Playwright fails).
        # Fall back to plain playwright if patchright isn't installed.
        async_playwright = None
        try:
            from patchright.async_api import async_playwright as _ap
            async_playwright = _ap
            logger.info("[browser] using patchright (anti-detect Playwright)")
        except ImportError:
            try:
                from playwright.async_api import async_playwright as _ap
                async_playwright = _ap
                logger.warning(
                    "[browser] patchright not installed — using stock playwright. "
                    "Riot will likely detect this. Run: "
                    ".venv\\Scripts\\pip install patchright && "
                    ".venv\\Scripts\\patchright install chromium"
                )
            except ImportError:
                raise AuthError(
                    "No browser engine installed. Run: "
                    ".venv\\Scripts\\pip install patchright && "
                    ".venv\\Scripts\\patchright install chromium"
                )

        if not self._captcha.enabled:
            raise CaptchaError(
                "No captcha provider configured. Set CAPSOLVER_KEY / "
                "RUCAPTCHA_KEY / TWOCAPTCHA_KEY in .env."
            )
        logger.info("[browser] captcha providers: %s",
                    self._captcha.provider_names)

        proxy = proxy or (settings.RIOT_PROXY or None)
        proxy_cfg = _playwright_proxy_cfg() if proxy else None
        if proxy_cfg:
            logger.info("[browser] using proxy: %s", proxy_cfg["server"])
        headless = bool(getattr(settings, "RIOT_HEADLESS", True))
        logger.info("[browser] headless=%s", headless)

        # Detect whether we're using patchright (anti-detect) or stock playwright
        is_patchright = "patchright" in async_playwright.__module__

        # Use a persistent profile so Cloudflare's cf_clearance cookie
        # survives between runs. After the first successful pass, subsequent
        # runs skip the JS challenge entirely.
        from pathlib import Path
        profile_dir = Path("riot_profiles") / "playwright_chrome"
        profile_dir.mkdir(parents=True, exist_ok=True)
        logger.info("[browser] persistent profile: %s", profile_dir)

        # Try real Chrome first (Cloudflare rarely blocks it), fall back to
        # bundled Chromium if Chrome isn't installed on the host.
        async with async_playwright() as pw:
            # patchright is already stealth — minimal args. stock playwright
            # needs every flag we can throw at it.
            if is_patchright:
                launch_args = []
            else:
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-infobars",
                    "--exclude-switches=enable-automation",
                ]
            ctx = None
            for channel_name in ("chrome", None):  # None = bundled chromium
                try:
                    launch_kwargs = {
                        "user_data_dir": str(profile_dir),
                        "headless": headless,
                        "proxy": proxy_cfg,
                        "args": launch_args,
                        "viewport": {"width": 1920, "height": 1080},
                        "locale": "en-US",
                        "user_agent": self.USER_AGENT,
                        "extra_http_headers": {
                            "Accept-Language": "en-US,en;q=0.9",
                        },
                    }
                    if channel_name:
                        launch_kwargs["channel"] = channel_name
                    ctx = await pw.chromium.launch_persistent_context(**launch_kwargs)
                    logger.info("[browser] launched %s",
                                channel_name or "bundled-chromium")
                    break
                except Exception as e:
                    logger.warning("[browser] %s launch failed: %s",
                                   channel_name or "chromium", str(e)[:120])
                    continue
            if ctx is None:
                raise AuthError(
                    "Could not launch any browser. "
                    "Install Chrome OR run `playwright install chromium`."
                )

            try:
                # patchright already injects proper stealth — adding our
                # manual init_script can actually flag the browser.
                if not is_patchright:
                    await ctx.add_init_script(self.STEALTH_INIT)
                # Reuse existing page if one came with the persistent context
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                page.set_default_timeout(45000)

                # Step 1: Land on Riot's homepage, then click Sign In so
                # Riot builds its own OAuth URL (no guesswork on our side).
                logger.info("[browser] navigating to riotgames.com")
                await self._goto_login_form(page)

                # Step 2: Wait for the username input.
                logger.info("[browser] waiting for login form…")
                await page.wait_for_selector(
                    'input[name="username"], input[autocomplete="username"]',
                    timeout=30000,
                )

                # Step 3: Fill credentials.
                logger.info("[browser] filling credentials")
                await page.fill(
                    'input[name="username"], input[autocomplete="username"]',
                    username,
                )
                await page.fill(
                    'input[name="password"], input[type="password"]',
                    password,
                )
                # Tiny human-ish pause
                await page.wait_for_timeout(500)

                # Step 4: Try to extract sitekey + rqdata (the form may have
                # an invisible hCaptcha attached). Solve preemptively.
                sitekey, rqdata = await self._extract_captcha_params(page)
                if sitekey:
                    logger.info("[browser] hCaptcha detected, solving "
                                "(sitekey=%s, rqdata=%s)",
                                sitekey, "yes" if rqdata else "no")
                    try:
                        token = await self._captcha.solve_hcaptcha(
                            sitekey=sitekey,
                            page_url=page.url,
                            invisible=True,
                            rqdata=rqdata,
                        )
                    except CaptchaError as e:
                        raise CaptchaRequired(f"captcha solve failed: {e}")
                    await self._inject_captcha_token(page, token)
                else:
                    logger.info("[browser] no hCaptcha sitekey on page yet — "
                                "will be triggered after submit")

                # Step 5: Submit the form.
                logger.info("[browser] submitting form")
                # Click any visible submit button; fallback to pressing Enter
                # in the password field.
                try:
                    await page.click(
                        'button[type="submit"], '
                        'button:has-text("Sign in"), '
                        'button:has-text("ВОЙТИ"), '
                        'button:has-text("Войти")',
                        timeout=5000,
                    )
                except Exception:
                    await page.press(
                        'input[name="password"], input[type="password"]',
                        "Enter",
                    )

                # Step 5b: After submit, Riot may pop up the captcha challenge.
                # Watch for it for up to 10s and solve if needed.
                await self._handle_post_submit_captcha(page)

                # Step 6: MFA?
                try:
                    await page.wait_for_selector(
                        'input[autocomplete="one-time-code"], '
                        'input[name="code"], input[name="multifactor"]',
                        timeout=5000,
                    )
                    if not mfa_code:
                        raise MfaRequired("MFA code required — re-run with code")
                    logger.info("[browser] entering MFA code")
                    await page.fill(
                        'input[autocomplete="one-time-code"], '
                        'input[name="code"], input[name="multifactor"]',
                        mfa_code,
                    )
                    await page.press(
                        'input[autocomplete="one-time-code"], '
                        'input[name="code"], input[name="multifactor"]',
                        "Enter",
                    )
                except MfaRequired:
                    raise
                except Exception:
                    pass  # no MFA prompt

                # Step 7: Wait for navigation to a Riot-internal post-login
                # page (account dashboard, valorant home, etc.). The
                # __Secure-access_token cookie is set during this redirect.
                logger.info("[browser] waiting for post-login redirect")
                await self._wait_for_logged_in(page)

                # Step 8: Read cookies.
                cookies = await ctx.cookies()
                jar = {c["name"]: c["value"] for c in cookies}
                logger.info("[browser] cookies after login: %s",
                            sorted(jar.keys()))
                access_token = jar.get("__Secure-access_token")
                id_token = jar.get("__Secure-id_token")
                ssid = jar.get("ssid", "")
                if not access_token or not id_token:
                    raise AuthError(
                        "Login finished but no RSO tokens in cookies. "
                        f"Got: {list(jar.keys())}"
                    )
            finally:
                try:
                    await ctx.close()
                except Exception:
                    pass

        # Step 9: Use the access_token to fetch entitlement + puuid + region
        # (lightweight API calls, reuse RsoAuth helpers + the same proxy).
        rso = RsoAuth()
        async with _make_session(proxy) as sess:
            _p = getattr(sess, "_rso_per_request_proxy", None)
            entitlement = await rso._fetch_entitlement(sess, access_token, _p)
            puuid = await rso._fetch_puuid(sess, access_token, _p)
            region = await rso._fetch_region(sess, access_token, id_token, _p)

        expires_at = (
            rso._decode_jwt_exp(access_token)
            or datetime.utcnow() + timedelta(hours=1)
        )
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
        logger.info("[browser] login OK puuid=%s… region=%s",
                    puuid[:8], region)
        return tokens

    # --------- internals ---------

    async def _goto_login_form(self, page) -> None:
        """Try entry URLs. account.riotgames.com auto-redirects to the login
        form (it always requires auth). For other entries we hunt the Sign-In
        link. Either way we end up on authenticate.riotgames.com."""
        last_err: Optional[Exception] = None
        for entry in self.ENTRY_URLS:
            try:
                logger.info("[browser] trying entry: %s", entry)
                await page.goto(entry, wait_until="domcontentloaded", timeout=30000)
                # Give the page (or any redirect) a beat to settle
                await page.wait_for_timeout(3000)

                # If Riot auto-redirected us to the login form, we're done.
                if "authenticate.riotgames.com" in page.url:
                    logger.info("[browser] auto-redirected to login: %s",
                                page.url[:120])
                    return

                # Otherwise try to find and click a Sign In link.
                clicked = await self._click_sign_in(page)
                if clicked:
                    await page.wait_for_url(
                        re.compile(r"authenticate\.riotgames\.com|"
                                   r"auth\.riotgames\.com/login"),
                        timeout=20000,
                    )
                    return

                # Dump diagnostic info so we know whether the page even loaded
                try:
                    info = await page.evaluate("""() => ({
                        url: location.href,
                        title: document.title,
                        bodyLen: (document.body?.innerText || '').length,
                        hasCloudflare: !!document.querySelector('[id*="cf-"], [class*="cf-"]'),
                        linkCount: document.querySelectorAll('a').length,
                        // Sample of links to debug selector mismatch
                        sampleHrefs: Array.from(document.querySelectorAll('a'))
                            .slice(0, 8).map(a => a.href).filter(Boolean),
                    })""")
                    logger.warning("[browser] %s: no sign-in found. %s",
                                   entry, info)
                except Exception:
                    pass
            except Exception as e:
                last_err = e
                logger.warning("[browser] entry %s failed: %s — trying next",
                               entry, str(e)[:120])
                continue

        raise Exception(
            f"All entry URLs failed (last error: {last_err}). "
            f"Last URL on page: {page.url}"
        )

    async def _click_sign_in(self, page) -> bool:
        """Find anything that links to the Riot auth flow and click it."""
        # Wait briefly for React to render header links
        try:
            await page.wait_for_selector(
                'a[href*="auth.riotgames.com"], a[href*="login"], '
                'a[href*="signin"], a[href*="sign-in"]',
                timeout=8000,
            )
        except Exception:
            pass
        # Try multiple strategies
        selectors = [
            'a[href*="auth.riotgames.com"]',
            'a[href*="authenticate.riotgames.com"]',
            'a[href*="sign-in"]',
            'a[href*="signin"]',
            'a:has-text("Sign in")',
            'a:has-text("Sign In")',
            'a:has-text("ВОЙТИ")',
            'a:has-text("Войти")',
            'button:has-text("Sign in")',
            'button:has-text("Войти")',
            'button:has-text("ВОЙТИ")',
        ]
        for sel in selectors:
            try:
                el = await page.query_selector(sel)
                if el:
                    href = await el.get_attribute("href") if "a[" in sel else None
                    logger.info("[browser] clicking sign-in via %s (href=%s)",
                                sel, (href or "")[:80])
                    await el.click()
                    return True
            except Exception:
                continue
        return False

    async def _extract_captcha_params(self, page) -> tuple[Optional[str], Optional[str]]:
        """Pull (sitekey, rqdata) from the live DOM."""
        try:
            result = await page.evaluate("""() => {
                const el = document.querySelector('[data-sitekey]') ||
                           document.querySelector('.h-captcha') ||
                           document.querySelector('iframe[src*="hcaptcha"]');
                let sitekey = el ? el.getAttribute('data-sitekey') : null;
                let rqdata = el ? el.getAttribute('data-rqdata') : null;
                if (!rqdata && window.hcaptcha && window.hcaptcha.getConfig) {
                    try {
                        const cfg = window.hcaptcha.getConfig();
                        if (cfg) { rqdata = rqdata || cfg.rqdata; sitekey = sitekey || cfg.sitekey; }
                    } catch (e) {}
                }
                if (!sitekey) {
                    const ifr = document.querySelector('iframe[src*="hcaptcha"]');
                    if (ifr) {
                        const m = ifr.src.match(/sitekey=([0-9a-f-]+)/i);
                        if (m) sitekey = m[1];
                    }
                }
                return {sitekey, rqdata};
            }""")
            return result.get("sitekey"), result.get("rqdata")
        except Exception as e:
            logger.warning("[browser] captcha param extraction failed: %s", e)
            return None, None

    async def _inject_captcha_token(self, page, token: str) -> None:
        """Inject the solved hCaptcha token into the page so submit succeeds."""
        try:
            await page.evaluate(
                """(token) => {
                    // Set response in any hidden hCaptcha field
                    const fields = document.querySelectorAll(
                        '[name="h-captcha-response"], #h-captcha-response, ' +
                        '[name="g-recaptcha-response"]'
                    );
                    fields.forEach(f => { f.value = token; });
                    // Many hCaptcha widgets also set a textarea
                    const ta = document.querySelector('textarea[name="h-captcha-response"]');
                    if (ta) ta.value = token;
                    // Notify hcaptcha JS that the challenge passed
                    if (window.hcaptcha && window.hcaptcha._setSiteKeyResponse) {
                        try { window.hcaptcha._setSiteKeyResponse(token); } catch(e) {}
                    }
                    // Some Riot variants store the token globally
                    window.__hcaptcha_response = token;
                }""",
                token,
            )
            logger.info("[browser] captcha token injected")
        except Exception as e:
            logger.warning("[browser] injecting captcha token failed: %s", e)

    async def _handle_post_submit_captcha(self, page) -> None:
        """If Riot showed the captcha challenge after submit, solve it."""
        try:
            await page.wait_for_selector(
                'iframe[src*="hcaptcha"], [data-sitekey]',
                timeout=8000,
            )
        except Exception:
            return  # no captcha popped
        # Re-extract because rqdata may have changed
        sitekey, rqdata = await self._extract_captcha_params(page)
        if not sitekey:
            return
        logger.info("[browser] post-submit captcha challenge — solving")
        try:
            token = await self._captcha.solve_hcaptcha(
                sitekey=sitekey,
                page_url=page.url,
                invisible=True,
                rqdata=rqdata,
            )
        except CaptchaError as e:
            raise CaptchaRequired(f"post-submit captcha failed: {e}")
        await self._inject_captcha_token(page, token)
        # Re-submit
        try:
            await page.click('button[type="submit"]', timeout=3000)
        except Exception:
            try:
                await page.press('input[type="password"]', "Enter")
            except Exception:
                pass

    async def _wait_for_logged_in(self, page) -> None:
        """Wait until Riot has redirected away from the login form.

        If the visible browser is up and a captcha challenge is showing,
        wait much longer (up to 3 min) so the user can solve it manually
        by clicking the images. This is the most reliable fallback when
        the captcha provider can't handle visible image challenges."""
        success_re = re.compile(
            r"(account|auth|www|playvalorant|leagueoflegends)\.?riot.*|"
            r"valorantesports|playvalorant\.com"
        )
        headless = bool(getattr(settings, "RIOT_HEADLESS", True))
        # 30 s in headless, 180 s if user can see the browser and solve manually
        total_wait = 30 if headless else 180
        deadline = asyncio.get_event_loop().time() + total_wait
        warned_manual = False
        while asyncio.get_event_loop().time() < deadline:
            url = page.url
            if "authenticate.riotgames.com" not in url and success_re.search(url):
                return
            # Surface form-level errors (wrong password etc.) immediately
            err_el = await page.query_selector(
                'text=/incorrect|invalid|wrong|неверн|ошибк/i'
            )
            if err_el:
                txt = (await err_el.inner_text())[:200]
                raise Exception(f"Login form shows error: {txt}")
            # If a captcha challenge is visible AND we're in GUI mode, prompt
            # the user to solve it by hand. (In headless we have no choice but
            # to fail.)
            has_captcha = await page.query_selector(
                'iframe[src*="hcaptcha"], iframe[title*="captcha"], '
                'iframe[title*="challenge"]'
            )
            if has_captcha and not warned_manual:
                if headless:
                    raise Exception(
                        "Visible hCaptcha challenge appeared but browser is "
                        "headless. Set RIOT_HEADLESS=False in .env so you can "
                        "solve the image challenge manually, or top up CapSolver."
                    )
                logger.warning(
                    "[browser] CAPTCHA CHALLENGE — solve it in the open "
                    "browser window. Waiting up to %ds for you to finish.",
                    total_wait,
                )
                warned_manual = True
            await asyncio.sleep(1)
        raise Exception(
            f"Login did not redirect away from authenticate.riotgames.com "
            f"within {total_wait}s. Last URL: {page.url}"
        )
