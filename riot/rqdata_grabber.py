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
from typing import Optional

logger = logging.getLogger(__name__)


async def grab_hcaptcha_params(page_url: str, timeout_seconds: int = 30) -> tuple[Optional[str], Optional[str]]:
    """Open page_url headlessly and return (sitekey, rqdata).
    rqdata may be None if the page doesn't expose it via DOM attributes."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.warning("[rqdata] playwright not installed — can't extract rqdata")
        return None, None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                ctx = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                )
                page = await ctx.new_page()
                page.set_default_timeout(timeout_seconds * 1000)
                await page.goto(page_url, wait_until="domcontentloaded")
                # React app mount + hCaptcha attach takes a few seconds
                await page.wait_for_timeout(3000)

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
