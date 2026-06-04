"""Extract hCaptcha Enterprise `rqdata` from Riot's authenticate page.

rqdata is a per-request enterprise challenge token that hCaptcha JS appends
to the captcha div *after* the page mounts. We can't get it from a plain
HTTP GET — it's set by JS. So we use a headless Playwright browser:
  1. Open the authenticate URL
  2. Wait for the React app to mount and hCaptcha widget to attach
  3. Read `data-rqdata` from the captcha element (or window.hcaptcha config)

With rqdata, captcha-solving services have a 2-3× higher success rate on
Riot's hCaptcha. Without it, 2captcha mostly times out.

Returns (sitekey, rqdata) or (sitekey, None) if rqdata couldn't be found.
"""
from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

from config import settings

logger = logging.getLogger(__name__)


def _build_playwright_proxy() -> Optional[dict]:
    """Convert RIOT_PROXY (.env URL) → Playwright proxy dict."""
    raw = (settings.RIOT_PROXY or "").strip()
    if not raw:
        return None
    u = urlparse(raw)
    if not u.hostname or not u.port:
        return None
    server = f"{u.scheme}://{u.hostname}:{u.port}"
    proxy_cfg = {"server": server}
    if u.username:
        proxy_cfg["username"] = u.username
    if u.password:
        proxy_cfg["password"] = u.password
    return proxy_cfg


async def grab_hcaptcha_params(page_url: str, timeout_seconds: int = 30) -> tuple[Optional[str], Optional[str]]:
    """Open page_url headlessly and return (sitekey, rqdata).
    rqdata may be None if the page doesn't expose it via DOM attributes."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("[rqdata] playwright not installed — can't extract rqdata")
        return None, None

    proxy_cfg = _build_playwright_proxy()
    if proxy_cfg:
        logger.info("[rqdata] using proxy: %s", proxy_cfg["server"])
    else:
        logger.warning(
            "[rqdata] no RIOT_PROXY set — Playwright will hit Riot from your "
            "real IP. If you're in RU/CIS, Riot will serve a blank/blocked "
            "page and rqdata will be missing → captcha will be rejected."
        )

    try:
        async with async_playwright() as pw:
            # Cloudflare/Riot detect headless Chromium via:
            #   - navigator.webdriver = true
            #   - missing window.chrome
            #   - HeadlessChrome in UA
            # Use --headless=new + disable automation flags + a real UA.
            # Allow user to disable headless via RIOT_HEADLESS=False in .env —
            # useful when Cloudflare still detects headless mode (rare, but
            # happens on heavily-protected pages).
            use_headless = bool(getattr(settings, "RIOT_HEADLESS", True))
            launch_kwargs = {
                "headless": use_headless,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-web-security",
                    "--disable-features=AutomationControlled",
                ],
            }
            if proxy_cfg:
                launch_kwargs["proxy"] = proxy_cfg
            browser = await pw.chromium.launch(**launch_kwargs)
            try:
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                    viewport={"width": 1920, "height": 1080},
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "Accept": (
                            "text/html,application/xhtml+xml,application/xml;"
                            "q=0.9,image/webp,*/*;q=0.8"
                        ),
                    },
                )
                # Hide webdriver flag — most basic anti-bot check
                await ctx.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
                    window.chrome = { runtime: {} };
                    Object.defineProperty(navigator, 'plugins', {
                        get: () => [1, 2, 3, 4, 5]
                    });
                    Object.defineProperty(navigator, 'languages', {
                        get: () => ['en-US', 'en']
                    });
                """)
                page = await ctx.new_page()
                page.set_default_timeout(timeout_seconds * 1000)
                # Warm up Cloudflare: visit riotgames.com first so the
                # browser gets baseline cookies + a non-bot reputation.
                # Going straight to authenticate.* from a clean profile
                # often trips the "automated tools" rule and returns Oops.
                try:
                    logger.info("[rqdata] warming up via riotgames.com first")
                    await page.goto("https://www.riotgames.com/",
                                    wait_until="domcontentloaded",
                                    timeout=20000)
                    await page.wait_for_timeout(2000)
                except Exception as e:
                    logger.warning("[rqdata] warmup failed: %s — continuing", e)
                await page.goto(page_url, wait_until="networkidle")
                # React app mount + hCaptcha attach takes time — wait for
                # the captcha iframe OR a visible password field (= form
                # fully rendered → hCaptcha will have attached too).
                try:
                    await page.wait_for_selector(
                        'iframe[src*="hcaptcha"], [data-sitekey], '
                        'input[type="password"]',
                        timeout=20000,
                    )
                except Exception:
                    logger.warning("[rqdata] no captcha/password selector after 20s")
                # Extra settle for hCaptcha JS to write rqdata into the DOM
                await page.wait_for_timeout(4000)
                # Quick visibility into what's on the page
                try:
                    snapshot = await page.evaluate("""() => ({
                        url: location.href,
                        hasHcaptchaIframe: !!document.querySelector('iframe[src*=\"hcaptcha\"]'),
                        hasDataSitekey: !!document.querySelector('[data-sitekey]'),
                        hasPasswordField: !!document.querySelector('input[type=\"password\"]'),
                        bodyLen: (document.body && document.body.innerText.length) || 0,
                    })""")
                    logger.info("[rqdata] snapshot: %s", snapshot)
                except Exception:
                    pass

                # Try to find the captcha element and pull data-rqdata
                # Multiple selectors because Riot's UI mutates
                result = await page.evaluate("""() => {
                    function findKey(obj, name, depth = 0) {
                        if (!obj || depth > 6) return null;
                        for (const k of Object.keys(obj || {})) {
                            if (k === name) return obj[k];
                        }
                        return null;
                    }
                    // 1. DOM data attributes
                    const el = document.querySelector('[data-sitekey]') ||
                               document.querySelector('.h-captcha') ||
                               document.querySelector('iframe[src*="hcaptcha"]');
                    let sitekey = el ? el.getAttribute('data-sitekey') : null;
                    let rqdata = el ? el.getAttribute('data-rqdata') : null;

                    // 2. Look in window for hcaptcha config
                    if (!rqdata && window.hcaptcha) {
                        try {
                            const cfg = window.hcaptcha.getConfig?.();
                            if (cfg) {
                                rqdata = rqdata || cfg.rqdata;
                                sitekey = sitekey || cfg.sitekey;
                            }
                        } catch (e) {}
                    }

                    // 3. Inspect iframe src for sitekey if missing
                    if (!sitekey) {
                        const iframe = document.querySelector('iframe[src*="hcaptcha"]');
                        if (iframe) {
                            const m = iframe.src.match(/sitekey=([0-9a-f-]+)/i);
                            if (m) sitekey = m[1];
                        }
                    }
                    return {sitekey, rqdata, url: location.href};
                }""")

                logger.info("[rqdata] extracted: sitekey=%s, rqdata=%s",
                            result.get("sitekey"),
                            "yes" if result.get("rqdata") else "no")
                return result.get("sitekey"), result.get("rqdata")
            finally:
                await browser.close()
    except Exception as e:
        logger.warning("[rqdata] extraction failed: %s", e)
        return None, None
