"""Factory for picking a MobileBackend based on .env (MOBILE_PROVIDER)."""
from __future__ import annotations

import logging

from config import settings
from mobile.interface import MobileBackend
from mobile.stub_backend import StubMobileBackend

logger = logging.getLogger(__name__)


def get_mobile_backend() -> MobileBackend:
    """Return the configured backend instance.

    Currently supports:
      stub      → no-op (default)
      emulator  → planned: Android emulator via Appium
      device    → planned: physical Android via ADB
    """
    provider = (settings.MOBILE_PROVIDER or "stub").lower().strip()

    if provider in ("stub", "", "none"):
        return StubMobileBackend()

    if provider == "emulator":
        try:
            from mobile.emulator_backend import EmulatorMobileBackend
            return EmulatorMobileBackend()
        except ImportError:
            logger.warning("emulator backend not built yet — falling back to stub")
            return StubMobileBackend()

    if provider == "device":
        try:
            from mobile.device_backend import DeviceMobileBackend
            return DeviceMobileBackend()
        except ImportError:
            logger.warning("device backend not built yet — falling back to stub")
            return StubMobileBackend()

    logger.warning("Unknown MOBILE_PROVIDER=%r — falling back to stub", provider)
    return StubMobileBackend()
