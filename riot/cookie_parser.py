"""Parse Riot cookies from various clipboard formats users might paste.

Supports:
  1. Raw "Cookie:" header value:  "a=1; b=2; c=3"
  2. JSON array (from Cookie-Editor extension): [{"name":"a","value":"1",...}, ...]
  3. JSON object (simple dict): {"a": "1", "b": "2"}
  4. cURL command: bot extracts the -b/--cookie part
"""
from __future__ import annotations

import json
import re
from typing import Optional


# Cookies that matter for account.riotgames.com session.
# Other cookies (analytics, etc.) can be present too — they don't hurt.
_RIOT_DOMAINS = (".riotgames.com", "riotgames.com", "account.riotgames.com",
                 "auth.riotgames.com", ".riot-games.com")


def parse_cookies(raw: str) -> Optional[dict[str, str]]:
    """Try multiple formats; return name→value dict, or None if unparseable."""
    raw = (raw or "").strip()
    if not raw:
        return None

    # JSON formats: parse strictly. Do NOT fall back to header parsing on
    # failure — that produces garbage cookies with embedded newlines.
    if raw.startswith(("{", "[")):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return _from_json(data)

    # cURL command: extract the -b/--cookie part
    m = re.search(r"(?:-b|--cookie)\s+['\"]([^'\"]+)['\"]", raw)
    if m:
        return _from_header_string(m.group(1))

    # Raw Cookie header value
    return _from_header_string(raw)


def _sanitize(cookies: dict[str, str]) -> dict[str, str]:
    """Drop cookies whose name/value contain characters illegal in headers."""
    clean: dict[str, str] = {}
    for k, v in cookies.items():
        if not k or any(ch in (k + v) for ch in ("\n", "\r", "\x00")):
            continue
        clean[k] = v
    return clean


def _from_json(data) -> Optional[dict[str, str]]:
    if isinstance(data, dict):
        # Plain dict like {"a": "1"}
        if all(isinstance(v, (str, int, float)) for v in data.values()):
            return {str(k): str(v) for k, v in data.items()}
        # Single cookie object
        if "name" in data and "value" in data:
            return {data["name"]: data["value"]}
        return None
    if isinstance(data, list):
        # Cookie-Editor style: [{"name":..,"value":..,"domain":..}, ...]
        result: dict[str, str] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            value = item.get("value")
            if name and value is not None:
                # Optional domain filter — accept cookies from riot domains
                domain = item.get("domain", "")
                if domain and not any(d in domain for d in (".riotgames", "riotgames")):
                    continue
                result[str(name)] = str(value)
        return result or None
    return None


def _from_header_string(s: str) -> Optional[dict[str, str]]:
    """Parse "name1=value1; name2=value2; ..." into a dict."""
    s = s.strip()
    # Strip a leading "Cookie:" / "cookie:" prefix if present
    if s.lower().startswith("cookie:"):
        s = s[7:].strip()
    if not s:
        return None
    result: dict[str, str] = {}
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, _, value = part.partition("=")
        name = name.strip()
        value = value.strip().strip('"')
        if name:
            result[name] = value
    return result or None


def cookies_to_playwright(cookies: dict[str, str], domain: str = ".riotgames.com") -> list[dict]:
    """Convert {name: value} dict to Playwright add_cookies() format.
    Used for header-string / simple-dict formats where we have no attributes."""
    return [
        {
            "name": name,
            "value": value,
            "domain": domain,
            "path": "/",
            "secure": True,
            "sameSite": "None",
        }
        for name, value in cookies.items()
    ]


def _map_same_site(v) -> str:
    """Map various sameSite spellings to Playwright's Strict/Lax/None."""
    if not v:
        return "Lax"
    s = str(v).lower()
    if s in ("no_restriction", "none"):
        return "None"
    if s == "strict":
        return "Strict"
    return "Lax"


def build_playwright_cookies(raw: str) -> list[dict]:
    """Build a full Playwright-ready cookie list from raw clipboard content,
    PRESERVING all attributes (domain, path, secure, httpOnly, sameSite,
    expires) when the input is a Cookie-Editor JSON export.

    This is critical for Riot: cookies named with the __Secure- prefix only
    work if they keep secure=true and the correct sameSite/httpOnly flags.
    """
    raw = (raw or "").strip()
    if not raw:
        return []

    # Cookie-Editor JSON array → preserve everything
    if raw.startswith("["):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, list):
            out: list[dict] = []
            for item in data:
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                value = item.get("value")
                if not name or value is None:
                    continue
                domain = item.get("domain") or ".riotgames.com"
                if "riotgames" not in domain:
                    continue
                ck = {
                    "name": str(name),
                    "value": str(value),
                    "domain": domain,
                    "path": item.get("path", "/"),
                    "secure": bool(item.get("secure", True)),
                    "httpOnly": bool(item.get("httpOnly", False)),
                    "sameSite": _map_same_site(item.get("sameSite")),
                }
                # __Secure-/__Host- prefixed cookies require secure=True
                if name.startswith(("__Secure-", "__Host-")):
                    ck["secure"] = True
                # expirationDate (float seconds) → Playwright "expires"
                exp = item.get("expirationDate") or item.get("expires")
                if isinstance(exp, (int, float)) and exp > 0:
                    ck["expires"] = int(exp)
                # Skip cookies with header-illegal chars in value
                if any(c in str(ck["value"]) for c in ("\n", "\r", "\x00")):
                    continue
                out.append(ck)
            return out

    # Fallback formats (header string / simple dict / cURL) — no attributes
    parsed = parse_cookies(raw)
    if not parsed:
        return []
    return cookies_to_playwright(parsed)
