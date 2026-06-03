"""QR-code decoder from images.

Used to read the QR-code that the buyer pastes into FunPay chat after
choosing "Sign in with QR" in Valorant. The QR payload is then handed off
to the Mobile module which approves the sign-in via Riot Mobile API.

QR data format for Riot Valorant client (observed):
  rso:qr:<short-token>?...
  https://valorant.riotgames.com/...?token=...
  Various URL-shaped payloads — we don't parse, we return raw text to the
  mobile module which knows what to do.

Uses opencv-python (cv2) for decoding — no external DLLs required on Windows.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path
from typing import Optional, Union

import cv2
import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_detector = cv2.QRCodeDetector()


def decode_qr_from_bytes(data: bytes) -> Optional[str]:
    """Decode the first QR-code found in raw image bytes.
    Returns the QR payload as a string, or None if no QR is found."""
    if not data:
        return None
    try:
        img = Image.open(io.BytesIO(data))
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
    """Return ALL QR codes found in the image."""
    if not data:
        return []
    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return []
    return _decode_all(img)


def _pil_to_cv2(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _decode(img: Image.Image) -> Optional[str]:
    """First pass; if nothing found, retry on 2x upscaled image."""
    res = _decode_all(img)
    if res:
        return res[0]
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
    mat = _pil_to_cv2(img)
    retval, decoded_list, _, straight_list = _detector.detectAndDecodeMulti(mat)
    if not retval:
        return []
    out: list[str] = []
    for text in decoded_list:
        if text:
            out.append(text)
    return out
