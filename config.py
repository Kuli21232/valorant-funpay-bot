from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    TELEGRAM_BOT_TOKEN: str
    TELEGRAM_ADMIN_ID: int

    FUNPAY_GOLDEN_KEY: str
    FUNPAY_PHPSESSID: str
    FUNPAY_USER_ID: int
    FUNPAY_POLL_INTERVAL: int = 15

    DATABASE_URL: str = "sqlite+aiosqlite:///./bot.db"
    AUTO_REPLY_TEXT: str = "Спасибо за заказ! Аккаунт будет выдан в ближайшее время."

    # Optional proxy for Telegram API (needed in Russia/restricted regions).
    # Examples:
    #   http://user:pass@host:port
    #   socks5://user:pass@host:port
    TELEGRAM_PROXY: str = ""

    # Optional proxy for FunPay HTTP requests.
    FUNPAY_PROXY: str = ""

    # Proxy for Riot API requests. CRITICAL in Russia/CIS: Riot blocks
    # datacenter IPs (VPN, GCP, AWS) with `invalid_request` regardless of
    # credentials. A residential proxy (BrightData/Smartproxy/IPRoyal) is
    # required for Riot login. Use SOCKS5 or HTTP, e.g.:
    #   socks5://user:pass@residential.proxy.com:1080
    #   http://user:pass@residential.proxy.com:8080
    RIOT_PROXY: str = ""

    # CapSolver API key — RECOMMENDED for Riot. Specialised in invisible
    # hCaptcha Enterprise which Riot uses. 2captcha can't reliably solve it.
    # Get from https://dashboard.capsolver.com — top up $3-5 for testing.
    CAPSOLVER_KEY: str = ""

    # 2captcha API key — fallback only. Usually times out on Riot's hCaptcha.
    # Get from https://2captcha.com/enterpage
    TWOCAPTCHA_KEY: str = ""

    # RuCaptcha API key — alternative fallback (ru-локализация 2captcha, совместимый API).
    # Поддерживает большой список типов капч. Получить на https://rucaptcha.com
    RUCAPTCHA_KEY: str = ""

    # Manual override for Riot's hCaptcha sitekey, in case auto-discovery fails.
    # Capture by opening authenticate.riotgames.com in Chrome DevTools → look
    # at hCaptcha iframe src for `sitekey=<UUID>`.
    RIOT_HCAPTCHA_SITEKEY: str = ""

    # Which mobile-control backend to use for Riot Mobile App operations
    # (logging in mobile, scanning the security QR, approving buyer QR).
    #   stub      — no-op, returns success. Used until a real backend is wired.
    #   emulator  — drive an Android emulator (LDPlayer/Genymotion) via Appium.
    #   device    — drive a physical phone over ADB.
    MOBILE_PROVIDER: str = "stub"

    # Riot login flow.
    # account.riotgames.com/security uses client_id=accountodactyl-prod with
    # security_profile=high — the strictest anti-bot. A login via a low-security
    # client (e.g. signup.leagueoflegends.com or prod-xsso-riotgames URL) is far
    # more permissive. After login, account.riotgames.com inherits the SSO
    # session via auth.riotgames.com cookies.
    #
    # Leave empty to use a sensible default (LoL signup page). If that also
    # gets blocked, paste a known-working URL (e.g. one from Valorant client).
    RIOT_LOGIN_URL: str = ""

    # Skip the headless Playwright rqdata grabber in RSO auth.
    # If you suspect the extra browser launch is poisoning the session
    # or getting flagged by Riot, set this to True.
    RIOT_SKIP_RQDATA: bool = False

    # Run Riot session-termination browser in headless mode.
    # Set False to see the browser window (helpful for debugging / first runs).
    RIOT_HEADLESS: bool = False
    RIOT_TIMEOUT_MS: int = 45000

    # IMAP credentials for auto-fetching Riot MFA codes from email.
    # When set, BrowserAuth reads the inbox after Riot triggers MFA,
    # extracts the 6-digit code, and types it into the form automatically.
    # Common hosts:
    #   Gmail:     imap.gmail.com  (use an "App password", not your Gmail one)
    #   Yandex:    imap.yandex.com
    #   firstmail: imap.firstmail.ltd
    #   Outlook:   outlook.office365.com
    IMAP_HOST: str = ""
    IMAP_PORT: int = 993
    IMAP_USER: str = ""
    IMAP_PASSWORD: str = ""


settings = Settings()
