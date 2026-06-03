"""Parse rental duration in minutes from FunPay order description."""
import re
from typing import Optional

DEFAULT_MINUTES = 60


def parse_duration_minutes(text: str) -> Optional[int]:
    """Extract rental duration from arbitrary text like:
       "1 час", "2 часа", "30 минут", "1h", "2 hours", "90 min".
    Returns minutes, or None if can't parse."""
    if not text:
        return None
    s = text.lower()

    # Hours patterns
    for pat in [
        r"(\d+)\s*ч(?:ас(?:а|ов)?)?\b",   # русский: 1 час, 2 часа, 5 часов, 3ч
        r"(\d+)\s*h(?:our|rs?)?\b",        # english: 1h, 2 hours, 3 hr
    ]:
        m = re.search(pat, s)
        if m:
            return int(m.group(1)) * 60

    # Day patterns
    for pat in [
        r"(\d+)\s*д(?:ень|ня|ней)?\b",    # 1 день, 2 дня
        r"(\d+)\s*day(?:s)?\b",            # 1 day, 2 days
    ]:
        m = re.search(pat, s)
        if m:
            return int(m.group(1)) * 60 * 24

    # Minutes patterns
    for pat in [
        r"(\d+)\s*мин(?:ут(?:а|ы)?)?\b",  # 30 минут, 15 мин
        r"(\d+)\s*min(?:ute)?(?:s)?\b",    # 30 min, 15 minutes
    ]:
        m = re.search(pat, s)
        if m:
            return int(m.group(1))

    return None
