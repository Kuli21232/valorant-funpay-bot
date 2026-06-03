import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

from config import settings

CaptchaCallback = Optional[Callable[[str], Awaitable[None]]]

logger = logging.getLogger(__name__)

_SECURITY_URL = "https://account.riotgames.com/security"
_SCREENSHOT_DIR = Path("riot_debug")
_STORAGE_DIR = Path("riot_sessions")
_PROFILE_DIR = Path("riot_profiles")  # persistent Chrome profiles per account


# Fallback login URLs. The state/code_challenge in custom URLs is server-side
# nonce — we can't generate our own; only Riot's official OAuth flow can.
# These URLs let Riot initiate a fresh OAuth flow themselves.
_FALLBACK_LOGIN_URLS = [
    "https://auth.riotgames.com/login",
    "https://account.riotgames.com/",
]


def _login_entry_url() -> str:
    """The URL we navigate to for login.
    Priority: RIOT_LOGIN_URL from .env > generic auth.riotgames.com entry."""
    custom = (settings.RIOT_LOGIN_URL or "").strip()
    if custom:
        return custom
    return _FALLBACK_LOGIN_URLS[0]

# Realistic browser fingerprint
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class RiotSessionManager:
    CAPTCHA_WAIT_SECONDS = 300  # 5 min for manual CAPTCHA solving
    MANUAL_LOGIN_WAIT_SECONDS = 600  # 10 min for full manual login fallback

    async def terminate_with_cookies(
        self, username: str, raw_cookies: str,
        password: Optional[str] = None, headed: bool = False
    ) -> bool:
        """Terminate Riot sessions using cookies copied from user's own browser.

        This is the FunPay-style approach: instead of logging in, we inject
        cookies into a fresh Playwright context (as if the user just opened
        the page in their normal browser), then click 'Sign out everywhere'.
        No login screen, no CAPTCHA, no fingerprint check — Riot sees an
        already-authenticated session and lets us in.

        `raw_cookies` is the original clipboard text (Cookie-Editor JSON,
        header string, or cURL) — we preserve all cookie attributes.
        """
        from riot.cookie_parser import build_playwright_cookies
        pw_cookies = build_playwright_cookies(raw_cookies)
        if not pw_cookies:
            logger.error("No usable cookies parsed for %s", username)
            return False
        logger.info("Terminating via cookies for %s (%d cookies)", username, len(pw_cookies))
        _SCREENSHOT_DIR.mkdir(exist_ok=True)
        timeout = settings.RIOT_TIMEOUT_MS

        try:
            async with async_playwright() as pw:
                logger.info("Launching browser (this can take 10-30s on first run)...")
                browser, context, page = await self._build_browser(
                    pw, storage_state=None, force_headed=headed
                )

                # Inject cookies (full attributes preserved) BEFORE navigation
                await context.add_cookies(pw_cookies)
                logger.info("Injected %d cookies. Opening account.riotgames.com/security ...", len(pw_cookies))

                try:
                    await page.goto(_SECURITY_URL, timeout=timeout, wait_until="domcontentloaded")
                    logger.info("Page loaded, URL: %s", page.url)
                    await page.wait_for_timeout(2500)
                    await self._dismiss_cookie_banner(page)

                    url = page.url
                    if "authenticate" in url or "login" in url or "authorize" in url:
                        # /security is a HIGH-security page (acr=urn:riot:gold).
                        # Our cookie session is bronze-level, so Riot requires a
                        # password step-up. Because the device is already trusted
                        # (tdid cookie), this re-auth usually has NO CAPTCHA.
                        logger.info(
                            "Step-up auth required (high-security page). "
                            "Re-entering password with trusted session..."
                        )
                        if not password:
                            await self._dump(page, username, "stepup_no_password")
                            logger.error(
                                "Step-up required but no password provided. "
                                "Cookie-only mode can't pass the high-security page."
                            )
                            await browser.close()
                            return False

                        ok = await self._do_login(page, username, password)
                        if not ok:
                            await self._dump(page, username, "stepup_login_failed")
                            logger.error("Step-up login form fill failed")
                            await browser.close()
                            return False

                        # Wait for redirect back to account/security
                        await page.wait_for_timeout(3000)
                        if await self._has_captcha(page):
                            await self._dump(page, username, "stepup_captcha")
                            logger.error("CAPTCHA on step-up (unexpected with trusted device)")
                            await browser.close()
                            return False

                        solved = await self._wait_for_login_complete(page, timeout)
                        if not solved:
                            await self._dump(page, username, "stepup_incomplete")
                            logger.error("Step-up auth did not complete. URL: %s", page.url)
                            await browser.close()
                            return False

                        # Ensure we're on the security page
                        await page.goto(_SECURITY_URL, timeout=timeout, wait_until="domcontentloaded")
                        await page.wait_for_timeout(2000)
                        logger.info("Step-up complete, on security page: %s", page.url)

                    # We're logged in (either via cookies or after step-up).
                    # Click "Sign out everywhere"
                    clicked = await self._click_sign_out_everywhere(page)
                    if clicked:
                        await page.wait_for_timeout(2500)
                        logger.info("Sessions terminated for %s (cookie mode)", username)
                        await browser.close()
                        return True
                    else:
                        await self._dump(page, username, "no_signout_button")
                        logger.error("Sign out button not found for %s", username)
                        await browser.close()
                        return False
                except Exception as e:
                    await self._dump(page, username, "cookie_error")
                    logger.exception("Cookie-mode terminate failed: %s", e)
                    await browser.close()
                    return False
        except Exception as e:
            logger.exception("Playwright launch failed for cookie-mode: %s", e)
            return False

    @staticmethod
    def storage_path_for(username: str) -> str:
        _STORAGE_DIR.mkdir(exist_ok=True)
        safe = username.replace("#", "_").replace("/", "_").replace("\\", "_")
        return str(_STORAGE_DIR / f"{safe}.json")

    @staticmethod
    def profile_dir_for(username: str) -> str:
        """Path to a persistent user-data-dir for this account.
        Using a persistent profile makes the browser indistinguishable from
        a real user's Chrome, which is critical for Riot's fingerprint check."""
        _PROFILE_DIR.mkdir(exist_ok=True)
        safe = username.replace("#", "_").replace("/", "_").replace("\\", "_")
        path = _PROFILE_DIR / safe
        path.mkdir(exist_ok=True)
        return str(path.absolute())

    async def manual_login_and_save(self, username: str) -> Optional[str]:
        """Open Riot login in a *real* Chrome (persistent profile) and wait for
        the operator to log in manually. Uses launch_persistent_context with
        NO stealth scripts, so Riot's fingerprint check sees a genuine browser.

        Waits for either:
          - URL change to account.riotgames.com (success), OR
          - browser window closed by user (assume success → save anyway)

        Returns path to saved storage_state, or None on failure."""
        logger.info("MANUAL login mode (persistent profile) for %s", username)
        _SCREENSHOT_DIR.mkdir(exist_ok=True)

        profile_dir = self.profile_dir_for(username)
        logger.info("Using profile directory: %s", profile_dir)

        # Big banner with troubleshooting hints
        banner = (
            "\n" + "=" * 60 + "\n"
            f"  MANUAL LOGIN for: {username}\n"
            f"  Profile: {profile_dir}\n\n"
            f"  >>> Browser will open with a clean Chrome profile.\n"
            f"  >>> Navigate to ANY URL that lets you log in:\n"
            f"      - https://account.riotgames.com/\n"
            f"      - https://playvalorant.com/\n"
            f"      - https://www.riotgames.com/\n"
            f"      - Or paste a fresh URL from Valorant client\n"
            f"  >>> Log in, solve CAPTCHA if needed.\n"
            f"  >>> When done — CLOSE THE WINDOW (X button).\n"
            f"  >>> Session is saved automatically on close.\n\n"
            f"  If 'Oops! Something went wrong' appears:\n"
            f"   - The OAuth state expired or Riot rate-limited your IP\n"
            f"   - Wait 30-60 minutes, try again with a different URL\n"
            f"   - Or use Riot Client (desktop) to get a fresh URL\n\n"
            f"  No timeout — bot waits until you close the window.\n"
            + "=" * 60 + "\n"
        )
        print(banner, flush=True)
        try:
            import winsound
            for _ in range(3):
                winsound.Beep(1000, 300)
        except Exception:
            pass

        try:
            async with async_playwright() as pw:
                # Persistent context — uses a real user-data-dir, no CDP automation
                # markers in the browser fingerprint.
                context = None
                for channel in ("chrome", "msedge", None):
                    try:
                        launch_args = dict(
                            user_data_dir=profile_dir,
                            headless=False,
                            viewport={"width": 1280, "height": 800},
                            locale="en-US",
                            timezone_id="Europe/Moscow",
                            user_agent=_USER_AGENT,
                            # IMPORTANT: keep automation flag out of args, no stealth scripts
                            ignore_default_args=["--enable-automation"],
                            args=["--no-default-browser-check"],
                        )
                        if channel:
                            launch_args["channel"] = channel
                        context = await pw.chromium.launch_persistent_context(**launch_args)
                        logger.info("Launched persistent context channel=%s", channel or "bundled")
                        break
                    except Exception as e:
                        logger.debug("Persistent context %s failed: %s", channel, e)
                        continue
                if context is None:
                    raise RuntimeError("Could not launch any Chrome variant for manual login")

                page = context.pages[0] if context.pages else await context.new_page()

                # In manual mode we do NOT navigate anywhere. The user opens
                # the URL they know works (Valorant client / fresh OAuth URL).
                # Goto-ing automatically often hits "Oops! Something went wrong"
                # because Riot's authenticate page is sensitive to referer +
                # session state we can't reproduce server-side.
                # If RIOT_LOGIN_URL is set in .env, navigate there as a hint.
                custom = (settings.RIOT_LOGIN_URL or "").strip()
                if custom:
                    logger.info("Opening custom URL from .env: %s", custom)
                    try:
                        await page.goto(custom, timeout=settings.RIOT_TIMEOUT_MS,
                                        wait_until="domcontentloaded")
                    except Exception as e:
                        logger.warning("Goto failed: %s", e)
                else:
                    logger.info(
                        "No RIOT_LOGIN_URL set — leaving browser on blank page. "
                        "Navigate manually to a working Riot login URL."
                    )
                    try:
                        # A neutral starting page — user can type URL into address bar
                        await page.goto("about:blank")
                    except Exception:
                        pass

                # Wait indefinitely until the user closes the browser window.
                # No URL polling, no timeout — pure manual investigation mode.
                close_event = asyncio.Event()
                context.on("close", lambda: close_event.set())

                logger.info(">>> Browser is open. Investigate freely. "
                            "Bot waits for you to close the window manually.")

                # Re-print reminder every 60s so it doesn't look frozen
                async def heartbeat():
                    while not close_event.is_set():
                        await asyncio.sleep(60)
                        if not close_event.is_set():
                            logger.info(">>> Still waiting for browser to be closed manually...")

                hb_task = asyncio.create_task(heartbeat())
                try:
                    await close_event.wait()
                finally:
                    hb_task.cancel()

                logger.info("Browser closed by user — saving session")

                # Try to save storage_state before context is fully gone
                state_path = self.storage_path_for(username)
                try:
                    await context.storage_state(path=state_path)
                    logger.info("Saved manual session: %s", state_path)
                except Exception as e:
                    logger.warning("Could not save storage_state (context closed): %s", e)
                    state_path = None

                # The persistent profile directory always contains the session
                # state (cookies, localStorage), even if storage_state.json
                # couldn't be exported. Return profile path so terminate_sessions
                # can reuse it via launch_persistent_context.
                return state_path or profile_dir
        except Exception as e:
            logger.exception("Manual login failed for %s: %s", username, e)
            return None

    async def _wait_for_url_or_close(self, page, close_event: asyncio.Event,
                                      timeout_ms: int) -> bool:
        """Wait until page URL hits account.riotgames.com, browser closes, or
        we hit the overall timeout. Returns True if success (URL matched or
        closed while on account page), False on timeout."""
        elapsed = 0
        step = 3000
        last_log = 0
        while elapsed < timeout_ms:
            if close_event.is_set():
                # Browser closed by user — assume they finished logging in.
                logger.info("Browser closed by user")
                return True
            try:
                url = page.url
                if "account.riotgames.com" in url and "authenticate" not in url and "authorize" not in url:
                    logger.info("Detected logged-in URL: %s", url)
                    # Give a couple of seconds for any final cookies to settle
                    await asyncio.sleep(2)
                    return True
            except Exception:
                # Page closed
                if close_event.is_set():
                    return True
            await asyncio.sleep(step / 1000)
            elapsed += step
            if elapsed - last_log >= 30000:
                remaining = (timeout_ms - elapsed) // 1000
                logger.info(">>> Waiting for manual login... %ds remaining.", remaining)
                last_log = elapsed
        return False

    async def login_and_save(
        self,
        username: str,
        password: str,
        on_captcha: CaptchaCallback = None,
    ) -> Optional[str]:
        """Login to Riot and persist browser session (cookies+localStorage).
        Returns path to saved storage_state, or None on failure."""
        logger.info("Login + save session for %s", username)
        _SCREENSHOT_DIR.mkdir(exist_ok=True)
        timeout = settings.RIOT_TIMEOUT_MS

        try:
            async with async_playwright() as pw:
                browser, context, page = await self._build_browser(pw, storage_state=None)
                try:
                    ok = await self._do_login_flow(page, username, password, timeout, on_captcha)
                    if not ok:
                        logger.error("Login failed for %s", username)
                        await browser.close()
                        return None

                    # Save session
                    state_path = self.storage_path_for(username)
                    await context.storage_state(path=state_path)
                    logger.info("Saved session for %s -> %s", username, state_path)
                    await browser.close()
                    return state_path
                except Exception as e:
                    logger.exception("Error in login_and_save for %s: %s", username, e)
                    await browser.close()
                    return None
        except Exception as e:
            logger.exception("Playwright failed for %s: %s", username, e)
            return None

    async def terminate_sessions(
        self,
        username: str,
        password: str,
        on_captcha: CaptchaCallback = None,
        storage_state: Optional[str] = None,
    ) -> bool:
        """Login to Riot and click 'Sign out everywhere'. Returns True on success.

        If CAPTCHA appears, on_captcha(username) is called (typically to notify
        admin via Telegram), and the bot waits up to CAPTCHA_WAIT_SECONDS for
        the admin to solve it manually in the visible browser window.
        """
        logger.info("Terminating sessions for %s", username)
        _SCREENSHOT_DIR.mkdir(exist_ok=True)
        timeout = settings.RIOT_TIMEOUT_MS

        # If storage_state points to a persistent profile directory, use that
        # instead of normal context — preserves all auth state from manual login.
        use_persistent = (
            storage_state and Path(storage_state).is_dir()
        )

        try:
            async with async_playwright() as pw:
                browser = None
                context = None
                if use_persistent:
                    for channel in ("chrome", "msedge", None):
                        try:
                            ctx_args = dict(
                                user_data_dir=storage_state,
                                headless=settings.RIOT_HEADLESS,
                                viewport={"width": 1280, "height": 800},
                                locale="en-US",
                                timezone_id="Europe/Moscow",
                                user_agent=_USER_AGENT,
                                ignore_default_args=["--enable-automation"],
                                args=["--no-default-browser-check"],
                            )
                            if channel:
                                ctx_args["channel"] = channel
                            context = await pw.chromium.launch_persistent_context(**ctx_args)
                            logger.info("Reusing persistent profile from manual login")
                            break
                        except Exception as e:
                            logger.debug("Persistent reuse %s failed: %s", channel, e)
                            continue
                    if context is None:
                        # Fall back to ephemeral context
                        use_persistent = False

                if not use_persistent:
                    browser, context, _ = await self._build_browser(pw, storage_state=storage_state)

                page = context.pages[0] if context.pages else await context.new_page()
                page.set_default_timeout(timeout)

                try:
                    success = await self._do_terminate(
                        page, username, password, timeout, on_captcha,
                        used_saved_session=storage_state is not None,
                    )
                except PlaywrightTimeout as e:
                    await self._dump(page, username, "timeout")
                    logger.error("Timeout for %s: %s", username, e)
                    success = False
                except Exception as e:
                    await self._dump(page, username, "error")
                    logger.exception("Error terminating %s: %s", username, e)
                    success = False

                if browser:
                    await browser.close()
                else:
                    await context.close()
                return success

        except Exception as e:
            logger.exception("Playwright launch failed for %s: %s", username, e)
            return False

    async def _build_browser(self, pw, storage_state: Optional[str] = None,
                              force_headed: bool = False):
        """Launch browser + context (real Chrome preferred, then Edge, then Chromium)
        with full stealth, optionally restoring a saved storage_state.
        If force_headed=True, headless setting is ignored — used for manual login.
        Returns (browser, context, page)."""
        browser = None
        headless = False if force_headed else settings.RIOT_HEADLESS
        for channel in ("chrome", "msedge", None):
            try:
                launch_args = dict(
                    headless=headless,
                    ignore_default_args=["--enable-automation"],
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ],
                )
                if channel:
                    launch_args["channel"] = channel
                browser = await pw.chromium.launch(**launch_args)
                logger.info("Launched browser channel=%s", channel or "bundled-chromium")
                break
            except Exception as e:
                logger.debug("Channel %s not available: %s", channel, e)
                continue
        if browser is None:
            raise RuntimeError("Could not launch any Chromium variant")

        ctx_kwargs = dict(
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="Europe/Moscow",
        )
        if storage_state and Path(storage_state).exists():
            ctx_kwargs["storage_state"] = storage_state
            logger.info("Restoring saved session: %s", storage_state)

        context = await browser.new_context(**ctx_kwargs)
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {
                get: () => [{name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'}]
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });
            window.chrome = { runtime: {}, app: {} };
            const origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) =>
                p.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : origQuery(p);
        """)
        page = await context.new_page()
        page.set_default_timeout(settings.RIOT_TIMEOUT_MS)
        return browser, context, page

    async def _do_login_flow(self, page, username, password, timeout, on_captcha) -> bool:
        """Just login, don't click 'Sign out everywhere'."""
        login_url = _login_entry_url()
        logger.info("Opening login URL (low-security flow): %s", login_url)
        await page.goto(login_url, timeout=timeout, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        await self._dismiss_cookie_banner(page)

        current_url = page.url
        if "account.riotgames.com" in current_url and "authenticate" not in current_url:
            logger.info("Already logged in via saved session")
            return True

        ok = await self._do_login(page, username, password)
        if not ok:
            return False

        # Wait for redirect with CAPTCHA awareness
        await page.wait_for_timeout(3000)
        if await self._has_credentials_error(page):
            if await self._has_migration_required(page):
                await self._dump(page, username, "migration_required")
                logger.error(
                    "Account %s requires migration to a Riot Account. "
                    "Log in manually via a real browser, complete the migration, "
                    "then re-run the bot.", username
                )
                return False
            await self._dump(page, username, "wrong_credentials")
            logger.error("Invalid username/password for %s", username)
            return False

        if await self._has_captcha(page):
            self._announce_captcha(username)
            if on_captcha is not None:
                for _ in range(3):
                    try:
                        await on_captcha(username)
                        break
                    except Exception:
                        await page.wait_for_timeout(2000)
            solved = await self._wait_for_login_complete(
                page, self.CAPTCHA_WAIT_SECONDS * 1000, manual_mode=True
            )
            return solved

        solved = await self._wait_for_login_complete(page, timeout)
        if not solved:
            if await self._has_captcha(page):
                self._announce_captcha(username)
                if on_captcha is not None:
                    for _ in range(3):
                        try:
                            await on_captcha(username)
                            break
                        except Exception:
                            await page.wait_for_timeout(2000)
                return await self._wait_for_login_complete(
                    page, self.CAPTCHA_WAIT_SECONDS * 1000, manual_mode=True
                )
            await self._dump(page, username, "login_failed")
        return solved

    # Selectors that work on the new authenticate.riotgames.com flow
    _USERNAME_SELECTORS = [
        'input[name="username"]',
        'input[autocomplete="username"]',
        'input[type="text"]:visible',
        'input[placeholder*="USERNAME" i]',
        'input[placeholder*="имя" i]',
    ]
    _PASSWORD_SELECTORS = [
        'input[name="password"]',
        'input[autocomplete="current-password"]',
        'input[type="password"]:visible',
    ]
    _SUBMIT_SELECTORS = [
        'button[type="submit"]',
        'button:has-text("Sign in")',
        'button:has-text("Войти")',
    ]

    async def _terminate_clicked(self, page, username: str) -> bool:
        """On the security page, find and click 'Sign out everywhere'."""
        try:
            await page.goto(_SECURITY_URL, timeout=settings.RIOT_TIMEOUT_MS,
                            wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
        except Exception as e:
            logger.error("Could not open security page after login: %s", e)
            return False

        # If still redirected to authenticate — session is invalid
        if "authenticate" in page.url or "authorize" in page.url:
            logger.error("Saved session is invalid (redirected to login)")
            return False

        # Find and click "Sign out everywhere"
        for sel in (
            'button:has-text("Sign out everywhere")',
            'button:has-text("Sign Out Everywhere")',
            'button:has-text("Выйти везде")',
            'button:has-text("Выйти из всех сеансов")',
            '[data-testid*="signout"]',
        ):
            try:
                btn = page.locator(sel).first
                if await btn.count() > 0:
                    await btn.click()
                    await page.wait_for_timeout(1500)
                    # Confirm dialog if present
                    for confirm_sel in (
                        'button:has-text("Sign out")',
                        'button:has-text("Confirm")',
                        'button:has-text("Выйти")',
                    ):
                        try:
                            cb = page.locator(confirm_sel).first
                            if await cb.count() > 0 and await cb.is_visible(timeout=1000):
                                await cb.click()
                                break
                        except Exception:
                            continue
                    logger.info("Clicked 'Sign out everywhere' for %s", username)
                    await page.wait_for_timeout(2000)
                    return True
            except Exception as e:
                logger.debug("Selector %s failed: %s", sel, e)
                continue
        logger.error("Could not find 'Sign out everywhere' button for %s", username)
        await self._dump(page, username, "no_signout_button")
        return False

    async def _do_terminate(
        self,
        page,
        username: str,
        password: str,
        timeout: int,
        on_captcha: CaptchaCallback = None,
        used_saved_session: bool = False,
    ) -> bool:
        logger.info("Opening %s", _SECURITY_URL)
        # Use domcontentloaded only — Riot SPA never reaches networkidle
        await page.goto(_SECURITY_URL, timeout=timeout, wait_until="domcontentloaded")

        # Small fixed wait for React app to mount
        await page.wait_for_timeout(2500)

        current_url = page.url
        logger.info("Current URL: %s", current_url)

        # Try to dismiss cookie consent if present (it can overlap clickable elements)
        await self._dismiss_cookie_banner(page)

        # If we're already on the account page (not auth/authenticate), we're logged in
        if "account.riotgames.com" in current_url and "authenticate" not in current_url and "authorize" not in current_url:
            logger.info("Already logged in")
        else:
            ok = await self._do_login(page, username, password)
            if not ok:
                return False

            # After submit, give the page a few seconds to react
            await page.wait_for_timeout(3000)

            # Check for "wrong credentials" error first — Riot shows error text
            # without leaving the login page.
            if await self._has_credentials_error(page):
                if await self._has_migration_required(page):
                    await self._dump(page, username, "migration_required")
                    logger.error(
                        "Account %s requires migration to a Riot Account. "
                        "Log in manually via a real browser, complete the migration, "
                        "then re-run the bot.", username
                    )
                    return False
                await self._dump(page, username, "wrong_credentials")
                logger.error("Invalid username/password for %s", username)
                return False

            # Detect CAPTCHA early — if it's visible, enter manual-solve mode
            if await self._has_captcha(page):
                self._announce_captcha(username)
                await self._dump(page, username, "captcha_appeared")
                if on_captcha is not None:
                    # Retry the callback a few times in case of TG network blip
                    for attempt in range(3):
                        try:
                            await on_captcha(username)
                            break
                        except Exception as cb_e:
                            logger.warning(
                                "on_captcha callback failed (attempt %d): %s",
                                attempt + 1, cb_e,
                            )
                            await page.wait_for_timeout(2000)

                # Wait for user to solve captcha. Success = URL changes to account page.
                solved = await self._wait_for_login_complete(
                    page, self.CAPTCHA_WAIT_SECONDS * 1000, manual_mode=True
                )
                if not solved:
                    await self._dump(page, username, "captcha_timeout")
                    logger.error(
                        "CAPTCHA not solved in %ds for %s",
                        self.CAPTCHA_WAIT_SECONDS, username,
                    )
                    return False
                logger.info("CAPTCHA solved — continuing")
            else:
                # No captcha — wait normally for redirect
                solved = await self._wait_for_login_complete(page, timeout)
                if not solved:
                    if await self._has_2fa(page):
                        await self._dump(page, username, "2fa_required")
                        logger.error("2FA required for %s", username)
                        return False
                    # Maybe captcha appeared late
                    if await self._has_captcha(page):
                        self._announce_captcha(username)
                        if on_captcha is not None:
                            for _ in range(3):
                                try:
                                    await on_captcha(username)
                                    break
                                except Exception:
                                    await page.wait_for_timeout(2000)
                        solved = await self._wait_for_login_complete(
                            page, self.CAPTCHA_WAIT_SECONDS * 1000
                        )
                        if not solved:
                            await self._dump(page, username, "captcha_timeout")
                            logger.error("CAPTCHA not solved for %s", username)
                            return False
                    else:
                        await self._dump(page, username, "login_failed")
                        logger.error("Login did not complete. URL=%s", page.url)
                        return False

            logger.info("Login successful — redirected to account page")

        logger.info("Navigating to security page...")
        await page.goto(_SECURITY_URL, timeout=timeout, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)
        await self._dismiss_cookie_banner(page)

        # Click "Sign out everywhere"
        logger.info("Looking for 'Sign out everywhere' button...")
        clicked = await self._click_sign_out_everywhere(page)
        if not clicked:
            await self._dump(page, username, "button_not_found")
            logger.error("'Sign out everywhere' button not found for %s", username)
            return False

        await page.wait_for_timeout(3000)
        await self._dump(page, username, "success")
        logger.info("Sessions terminated for %s", username)
        return True

    async def _wait_for_login_complete(self, page, timeout_ms: int, manual_mode: bool = False) -> bool:
        """Wait until URL changes to account.riotgames.com (not the auth host).
        Polls in 5-second increments so we can also bail on credential errors
        and log progress for long manual waits (CAPTCHA solving)."""
        elapsed = 0
        step = 5000  # 5 seconds
        last_log = 0
        while elapsed < timeout_ms:
            try:
                await page.wait_for_url(
                    lambda url: "account.riotgames.com" in url and "authenticate" not in url,
                    timeout=step,
                )
                return True
            except PlaywrightTimeout:
                elapsed += step
                # Bail if Riot now shows wrong-credentials error
                if await self._has_credentials_error(page):
                    logger.error("Credentials error appeared during wait")
                    return False
                # Log progress every 30 seconds during long waits
                if manual_mode and elapsed - last_log >= 30000:
                    remaining = (timeout_ms - elapsed) // 1000
                    logger.info(
                        ">>> Waiting for manual CAPTCHA solve... %ds remaining. "
                        "Look at the OPEN BROWSER WINDOW.",
                        remaining,
                    )
                    last_log = elapsed
        return False

    async def _do_login(self, page, username: str, password: str) -> bool:
        # Wait for the Sign in form to render
        try:
            await page.locator('text=/Sign in|Войти/').first.wait_for(
                state="visible", timeout=30000
            )
        except PlaywrightTimeout:
            logger.warning("'Sign in' heading not found within 30s, proceeding anyway")

        # Extra wait for React to mount the input fields (they appear after heading)
        try:
            await page.locator('input[name="username"]:visible, input[autocomplete="username"]:visible').first.wait_for(
                state="visible", timeout=15000
            )
        except PlaywrightTimeout:
            logger.warning("Username input not visible within 15s, proceeding anyway")

        # Find username field
        user_field = await self._find_username(page)
        if not user_field:
            await self._dump(page, username, "no_username_field")
            logger.error("Username field not found. URL=%s", page.url)
            return False

        logger.info("Setting username...")
        await user_field.click()
        # Use JS-based value setter — press_sequentially gets mangled by
        # non-US Windows keyboard layouts (e.g. RU layout maps physical X -> "ч").
        # This approach uses the React-friendly native value setter and
        # dispatches input/change events that React listens for.
        await self._react_safe_fill(user_field, username)

        # Find password field — sometimes it appears only after typing username
        pass_field = await self._find_password(page)
        if not pass_field:
            await self._dump(page, username, "no_password_field")
            logger.error("Password field not found")
            return False

        logger.info("Setting password...")
        await pass_field.click()
        await self._react_safe_fill(pass_field, password)

        # Tiny pause to let React commit state before submitting
        await page.wait_for_timeout(500)

        # Note: we DON'T check for CAPTCHA before submit — Riot keeps an invisible
        # hCaptcha/Arkose iframe in the DOM at all times. It only becomes a problem
        # if it visibly appears AFTER submit.

        # Submit — try Enter first (most reliable for SPA forms), then click
        await pass_field.press("Enter")
        logger.info("Submitted login (Enter key)...")

        # Riot's new UI uses a circular arrow button rather than a classic
        # submit button. If Enter didn't trigger the submit, click it explicitly.
        submit_clicked = await self._click_submit_arrow(page)
        if submit_clicked:
            logger.info("Also clicked the circular submit arrow")

        return True

    async def _click_submit_arrow(self, page) -> bool:
        """Click the circular arrow submit button on authenticate.riotgames.com.
        The new Riot login UI replaced the classic submit button with a circle
        containing a right-arrow SVG. Enter-key alone often fails to submit."""
        selectors = [
            'button[type="submit"]:visible',
            'button:has(> svg):visible',
            'button:has(> span > svg):visible',
            'button[class*="signin"]:visible',
            'button[class*="submit"]:visible',
            'button[aria-label*="sign" i]:visible',
            'form button:visible',
            'button:visible',
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel).first
                if await btn.count() == 0:
                    continue
                if not await btn.is_visible(timeout=500):
                    continue
                # Skip social-login buttons (they sit outside the form)
                text = (await btn.text_content() or "").lower()
                if any(s in text for s in ("facebook", "google", "apple", "xbox", "playstation")):
                    continue
                await btn.click(timeout=2000)
                return True
            except Exception:
                continue
        return False

    async def _react_safe_fill(self, locator, value: str) -> None:
        """Fill an input field safely on React forms regardless of OS keyboard
        layout.

        Uses page.keyboard.insert_text() — this sends text via CDP with
        isTrusted=true events, bypassing OS keyboard layout (so Latin chars
        are not mangled by Russian/Cyrillic layouts), AND React sees them as
        legitimate user input.
        """
        # Make sure the field is focused
        await locator.click()
        await locator.evaluate("el => el.focus()")

        # Clear existing content via Ctrl+A → Delete (works with any layout
        # because Ctrl+A is mapped at OS level by keycode, not character).
        page = locator.page
        await page.keyboard.press("Control+a")
        await page.keyboard.press("Delete")

        # insertText sends each character as a trusted input event via CDP,
        # without going through the OS keyboard layout.
        await page.keyboard.insert_text(value)

        # Force React to pick up the value by dispatching input/change events.
        await locator.evaluate(
            "el => { el.dispatchEvent(new Event('input', { bubbles: true }));"
            "        el.dispatchEvent(new Event('change', { bubbles: true })); }"
        )

    async def _find_username(self, page):
        """Find the username input by trying multiple strategies."""
        # Strategy 1: by label
        for label in ["USERNAME", "Username", "Имя пользователя"]:
            try:
                loc = page.get_by_label(label, exact=False).first
                if await loc.is_visible(timeout=2000):
                    return loc
            except Exception:
                continue
        # Strategy 2: combined selector across attributes
        combined = (
            'input[name="username"]:visible,'
            'input[autocomplete="username"]:visible,'
            'input[id="username"]:visible,'
            'input[type="text"]:visible,'
            'input[type="email"]:visible'
        )
        try:
            loc = page.locator(combined).first
            await loc.wait_for(state="visible", timeout=10000)
            return loc
        except Exception:
            pass
        # Strategy 3: first visible input on page (heuristic — username always first)
        try:
            inputs = page.locator("input:visible")
            count = await inputs.count()
            if count >= 1:
                first = inputs.nth(0)
                # Skip inputs of type checkbox/radio
                input_type = await first.get_attribute("type") or ""
                if input_type not in ("checkbox", "radio", "hidden", "submit"):
                    return first
        except Exception:
            pass
        return None

    async def _find_password(self, page):
        """Find password input."""
        for label in ["PASSWORD", "Password", "Пароль"]:
            try:
                loc = page.get_by_label(label, exact=False).first
                if await loc.is_visible(timeout=2000):
                    return loc
            except Exception:
                continue
        try:
            loc = page.locator('input[type="password"]:visible').first
            await loc.wait_for(state="visible", timeout=10000)
            return loc
        except Exception:
            pass
        # Try clicking a "Next" / "Continue" if it's a 2-step form
        for sel in [
            'button:has-text("Next")',
            'button:has-text("Continue")',
            'button:has-text("Далее")',
            'button[type="submit"]:visible',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1500):
                    await btn.click(timeout=2000)
                    logger.info("Clicked intermediate button: %s", sel)
                    break
            except Exception:
                continue
        try:
            loc = page.locator('input[type="password"]:visible').first
            await loc.wait_for(state="visible", timeout=8000)
            return loc
        except Exception:
            return None

    async def _dismiss_cookie_banner(self, page) -> None:
        for sel in [
            'button:has-text("Accept All")',
            'button:has-text("Accept all")',
            'button:has-text("Принять")',
            'button:has-text("Save")',
            '[data-testid="uc-accept-all-button"]',
        ]:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=1000):
                    await btn.click(timeout=2000)
                    logger.debug("Dismissed cookie banner via: %s", sel)
                    await page.wait_for_timeout(500)
                    return
            except Exception:
                continue

    async def terminate_all_accounts(
        self,
        accounts: list[dict],
        on_captcha: CaptchaCallback = None,
    ) -> dict[str, bool]:
        """Sequentially terminate sessions for multiple accounts."""
        results: dict[str, bool] = {}
        for acc in accounts:
            results[acc["username"]] = await self.terminate_sessions(
                acc["username"], acc["password"], on_captcha=on_captcha,
            )
        return results

    async def _click_sign_out_everywhere(self, page) -> bool:
        selectors = [
            'button:has-text("Sign out everywhere")',
            'button:has-text("Sign Out Everywhere")',
            'button:has-text("Выйти везде")',
            'button:has-text("Выйти из всех сессий")',
            '[data-testid="signout-all"]',
            '[data-testid="sign-out-all"]',
        ]
        for selector in selectors:
            try:
                btn = page.locator(selector).first
                await btn.wait_for(state="visible", timeout=5000)
                await btn.click()
                logger.info("Clicked: %s", selector)
                # Look for confirm button
                await self._confirm_dialog(page)
                return True
            except PlaywrightTimeout:
                continue
            except Exception as e:
                logger.debug("Selector %s failed: %s", selector, e)
                continue
        return False

    async def _confirm_dialog(self, page) -> None:
        await page.wait_for_timeout(500)
        confirm_selectors = [
            'button:has-text("Sign out")',
            'button:has-text("Выйти")',
            'button:has-text("Confirm")',
            'button:has-text("Подтвердить")',
            'button:has-text("Yes")',
            'button:has-text("Да")',
        ]
        for selector in confirm_selectors:
            try:
                btn = page.locator(selector).first
                await btn.wait_for(state="visible", timeout=2000)
                await btn.click()
                logger.info("Confirmed via: %s", selector)
                return
            except PlaywrightTimeout:
                continue
            except Exception:
                continue

    async def _has_credentials_error(self, page) -> bool:
        """Check if Riot is showing 'wrong username or password' message."""
        try:
            html = (await page.content()).lower()
        except Exception:
            return False
        error_phrases = [
            "username or password may be incorrect",
            "username or password is incorrect",
            "incorrect username or password",
            "имя пользователя или пароль",
            "неверн",
        ]
        return any(phrase in html for phrase in error_phrases)

    async def _has_migration_required(self, page) -> bool:
        """Detect the 'update to a Riot Account' banner.
        Old LoL accounts that haven't been migrated block automated login."""
        try:
            html = (await page.content()).lower()
        except Exception:
            return False
        return "update to a riot account" in html or "haven't played in a few months" in html

    async def _has_captcha(self, page) -> bool:
        """Detect VISIBLE captcha that requires manual solving.
        Strict — only large visible challenge iframes/widgets count."""
        indicators = [
            "iframe[src*='hcaptcha-challenge']",
            "iframe[title*='hCaptcha challenge' i]",
            "iframe[title*='recaptcha challenge' i]",
            "iframe[id*='arkoselabs'][title]",
            "div.h-captcha iframe",
            "[data-testid='captcha-challenge']",
        ]
        for sel in indicators:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                try:
                    if not await loc.is_visible(timeout=500):
                        continue
                    box = await loc.bounding_box()
                    # Real CAPTCHA challenges are at least 250×250
                    if box and box["width"] > 250 and box["height"] > 250:
                        return True
                except Exception:
                    continue
            except Exception:
                continue
        return False

    async def _has_2fa(self, page) -> bool:
        indicators = [
            'input[name="code"]',
            'input[autocomplete="one-time-code"]',
            "[data-testid='multifactor']",
        ]
        for sel in indicators:
            try:
                if await page.locator(sel).count() > 0:
                    return True
            except Exception:
                continue
        return False

    def _announce_captcha(self, username: str) -> None:
        """Big obvious console message + sound beep so the operator notices."""
        banner = (
            "\n"
            "============================================================\n"
            f"  CAPTCHA detected for: {username}\n"
            "  >>> SWITCH TO THE OPEN BROWSER WINDOW AND SOLVE IT  <<<\n"
            f"  You have {self.CAPTCHA_WAIT_SECONDS // 60} minutes.\n"
            "============================================================\n"
        )
        # Print directly so it's visible even if logging level is off
        print(banner, flush=True)
        logger.warning("CAPTCHA appeared for %s — waiting for manual solve", username)
        # Sound alert on Windows
        try:
            import winsound
            for _ in range(3):
                winsound.Beep(1000, 300)
        except Exception:
            # Fallback: ASCII bell
            print("\a", end="", flush=True)

    async def _dump(self, page, username: str, tag: str) -> None:
        """Save screenshot + URL for debugging."""
        try:
            safe_user = username.replace("#", "_").replace("/", "_")
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            path = _SCREENSHOT_DIR / f"{safe_user}_{tag}_{ts}.png"
            await page.screenshot(path=str(path), full_page=True)
            logger.info("Saved screenshot: %s (URL: %s)", path, page.url)
        except Exception as e:
            logger.debug("Could not save screenshot: %s", e)
