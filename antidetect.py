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

_profiles: dict[str, dict] = {}


def get_profile(profile_path: str = "") -> dict:
    path = profile_path or "browser_profile.json"
    if path not in _profiles:
        _profiles[path] = load_or_create_profile(path)
    return _profiles[path]


async def setup_stealth_on_new_page(page, profile_path: str = "") -> None:
    """Pre-navigation stealth via CDP addScriptToEvaluateOnNewDocument."""
    profile = get_profile(profile_path)
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

    # Method 3: evaluate fallback is DANGEROUS — stealth lost on navigation.
    # Store script so we can re-inject after each navigation.
    page._stealth_script = script
    try:
        await page.evaluate(script)
        logger.warning("Stealth via evaluate only — will re-inject on navigation. addScriptToEvaluateOnNewDocument failed.")
    except Exception as e:
        logger.error("ALL stealth injection failed: %s — browser is detectable!", e)


def get_chrome_args(profile_path: str = "") -> list[str]:
    """Chrome args from persistent profile."""
    profile = get_profile(profile_path)
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
        "--disable-background-networking",
        "--disable-default-apps",
        "--disable-sync",
        "--disable-translate",
        "--metrics-recording-only",
        "--mute-audio",
        "--renderer-process-limit=2",
        "--disable-background-timer-throttling",
        "--js-flags=--max-old-space-size=256",
    ]
