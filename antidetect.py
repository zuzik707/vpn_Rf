"""
Anti-detection модуль v3.

Expert approach: persistent fingerprint profile + minimal overrides.
Each Object.defineProperty is a potential detection vector,
so we only patch what nodriver doesn't already handle.
"""

import logging

import nodriver
import nodriver.cdp.page

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
    profile = get_profile()
    script = build_stealth_script(profile)

    # Method 1: raw CDP command (works across nodriver versions)
    try:
        await page.send(
            nodriver.cdp.page.add_script_to_evaluate_on_new_document(source=script)
        )
        logger.info("Stealth injected via CDP addScript (%d bytes)", len(script))
        return
    except Exception as e:
        logger.debug("CDP addScript method 1 failed: %s", e)

    # Method 2: browser-level connection
    try:
        if hasattr(page, '_browser') and page._browser:
            conn = page._browser.connection
        elif hasattr(page, 'browser') and page.browser:
            conn = page.browser.connection
        else:
            conn = None
        if conn:
            await conn.send(
                nodriver.cdp.page.add_script_to_evaluate_on_new_document(source=script),
                target_id=page.target.target_id
            )
            logger.info("Stealth injected via browser connection (%d bytes)", len(script))
            return
    except Exception as e:
        logger.debug("CDP addScript method 2 failed: %s", e)

    # Method 3: direct evaluate as fallback (runs once, not on new documents)
    try:
        await page.evaluate(script)
        logger.info("Stealth injected via evaluate fallback (%d bytes)", len(script))
    except Exception as e:
        logger.warning("All stealth injection methods failed: %s", e)


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
        # Low-memory VPS optimizations (1 CPU / 1 GB RAM)
        "--disable-gpu",
        "--disable-dev-shm-usage",
        "--no-sandbox",
        "--disable-extensions",
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-translate",
        "--metrics-recording-only",
        "--mute-audio",
        "--no-zygote",
        "--single-process",
        "--js-flags=--max-old-space-size=256",
    ]
