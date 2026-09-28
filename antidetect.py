"""
Anti-detection модуль v3.

Expert approach: persistent fingerprint profile + minimal overrides.
Each Object.defineProperty is a potential detection vector,
so we only patch what nodriver doesn't already handle.
"""

import logging

from fingerprint import load_or_create_profile, build_stealth_script

logger = logging.getLogger(__name__)

_profile = None


def get_profile() -> dict:
    global _profile
    if _profile is None:
        _profile = load_or_create_profile()
    return _profile


async def setup_stealth_on_new_page(page) -> None:
    """Pre-navigation stealth via CDP addScriptToEvaluateOnNewDocument."""
    try:
        import nodriver.cdp.page as cdp_page
        profile = get_profile()
        script = build_stealth_script(profile)
        await page.send(cdp_page.add_script_to_evaluate_on_new_document(source=script))
        logger.info("Stealth injected (profile-based, %d bytes)", len(script))
    except Exception as e:
        logger.warning("CDP addScriptToEvaluateOnNewDocument failed: %s", e)


def get_chrome_args() -> list[str]:
    """Chrome args from persistent profile."""
    profile = get_profile()
    width, height = profile["viewport"]
    return [
        f"--window-size={width},{height}",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--lang=en-US",
        "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        "--enforce-webrtc-ip-permission-check",
    ]
