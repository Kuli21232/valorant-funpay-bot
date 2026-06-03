"""QR-code decoder from images.

Used to read the QR-code that the buyer pastes into FunPay chat after
choosing "Sign in with QR" in Valorant. The QR payload is then handed off
to the Mobile module which approves the sign-in via Riot Mobile API.

QR data format for Riot Valorant client (observed):
  rso:qr:<short-token>?...
  https://valorant.riotgames.com/...?token=...
  Various URL-shaped payloads — we don't parse, we return raw text to the
  mobile module which knows what to do.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional, Union

from PIL import Image
from pyzbar import pyzbar

logger = logging.getLogger(__name__)


def decode_qr_from_bytes(data: bytes) -> Optional[str]:
    """Decode the first QR-code found in raw image bytes.
    Returns the QR payload as a string, or None if no QR is found / image
    is unreadable."""
    if not data:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        # Convert palette/RGBA images to RGB for pyzbar
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
    except Exception as e:
        logger.warning("decode_qr: failed to open image: %s", e)
        return None
    return _decode(img)


def decode_qr_from_file(path: Union[str, Path]) -> Optional[str]:
    """Decode the first QR-code in an image file (jpg/png/bmp/etc)."""
    try:
        img = Image.open(path)
        if img.mode not in ("L", "RGB"):
            img = img.convert("RGB")
    except Exception as e:
        logger.warning("decode_qr: failed to open %s: %s", path, e)
        return None
    return _decode(img)


def decode_all_from_bytes(data: bytes) -> list[str]:
    """Return ALL QR codes found in the image (useful for screenshots
    that contain multiple codes / surrounding UI)."""
    if not data:
        return []
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return []
    return _decode_all(img)


def _decode(img: Image.Image) -> Optional[str]:
    """First pass on the image; if nothing, try upscaling (QR can be tiny)."""
    res = _decode_all(img)
    if res:
        return res[0]
    # Retry on upscaled image — buyer screenshots are often small.
    try:
        w, h = img.size
        up = img.resize((w * 2, h * 2), Image.LANCZOS)
        res = _decode_all(up)
        if res:
            return res[0]
    except Exception:
        pass
    return None


def _decode_all(img: Image.Image) -> list[str]:
    found = pyzbar.decode(img)
    out: list[str] = []
    for d in found:
        if d.type == "QRCODE":
            try:
                out.append(d.data.decode("utf-8", errors="replace"))
            except Exception:
                continue
    return out
