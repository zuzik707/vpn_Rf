"""
Persistent browser fingerprint profile.

Real anti-detect approach: one account = one identity = one fingerprint.
Randomizing per-session is a detection vector — Cloudflare sees same
account + same IP + different browser = bot.

Profile is saved to disk and reused across restarts.
"""

import json
import logging
import os
import random

logger = logging.getLogger(__name__)

PROFILE_PATH = "browser_profile.json"

DEFAULT_PROFILES = [
    {
        "viewport": [1920, 1080],
        "webgl_vendor": "Google Inc. (NVIDIA Corporation)",
        "webgl_renderer": "ANGLE (NVIDIA Corporation, NVIDIA GeForce GTX 1060 6GB/PCIe/SSE2, OpenGL 4.5.0)",
        "hardware_concurrency": 8,
        "device_memory": 8,
        "platform": "Linux x86_64",
        "canvas_seed": None,
    },
    {
        "viewport": [1536, 864],
        "webgl_vendor": "Google Inc. (Intel)",
        "webgl_renderer": "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)",
        "hardware_concurrency": 4,
        "device_memory": 8,
        "platform": "Linux x86_64",
        "canvas_seed": None,
    },
    {
        "viewport": [1366, 768],
        "webgl_vendor": "Google Inc. (AMD)",
        "webgl_renderer": "ANGLE (AMD, AMD Radeon RX 580 Series (polaris10, LLVM 15.0.7, DRM 3.49, 6.1.0), OpenGL 4.6)",
        "hardware_concurrency": 6,
        "device_memory": 16,
        "platform": "Linux x86_64",
        "canvas_seed": None,
    },
]


def load_or_create_profile(path: str = PROFILE_PATH) -> dict:
    """Load existing profile or create a new persistent one."""
    if os.path.exists(path):
        try:
            with open(path) as f:
                profile = json.load(f)
            logger.info("Fingerprint profile loaded: %s (%dx%d)",
                        path, profile["viewport"][0], profile["viewport"][1])
            return profile
        except Exception as e:
            logger.warning("Profile load failed: %s — creating new", e)

    profile = random.choice(DEFAULT_PROFILES).copy()
    profile["canvas_seed"] = random.randint(1, 999999)
    profile["connection_rtt"] = random.choice([50, 75, 100])
    profile["timezone_offset"] = 5 * 60

    try:
        with open(path, "w") as f:
            json.dump(profile, f, indent=2)
        logger.info("New fingerprint profile created: %dx%d, %s",
                    profile["viewport"][0], profile["viewport"][1],
                    profile["webgl_renderer"][:40])
    except Exception as e:
        logger.warning("Profile save failed: %s", e)

    return profile


def build_stealth_script(profile: dict) -> str:
    """Build minimal stealth JS from profile. Fewer overrides = less detection."""
    vp = profile["viewport"]
    seed = profile.get("canvas_seed", 42)
    rtt = profile.get("connection_rtt", 50)
    hc = profile.get("hardware_concurrency", 8)
    dm = profile.get("device_memory", 8)
    vendor = profile["webgl_vendor"]
    renderer = profile["webgl_renderer"]

    return f"""
    // --- 1. webdriver cleanup (nodriver handles most, safety net) ---
    Object.defineProperty(navigator, 'webdriver', {{get: () => undefined, configurable: true}});
    try {{ delete navigator.__proto__.webdriver; }} catch(e) {{}}

    // --- 2. WebGL fingerprint (must match Linux platform) ---
    const _patchGL = (proto) => {{
        const orig = proto.getParameter;
        proto.getParameter = function(p) {{
            if (p === 37445) return '{vendor}';
            if (p === 37446) return '{renderer}';
            return orig.call(this, p);
        }};
    }};
    _patchGL(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') _patchGL(WebGL2RenderingContext.prototype);

    // --- 3. Hardware specs (must be plausible for claimed GPU) ---
    Object.defineProperty(navigator, 'hardwareConcurrency', {{get: () => {hc}}});
    Object.defineProperty(navigator, 'deviceMemory', {{get: () => {dm}}});

    // --- 4. chrome.runtime stub ---
    if (!window.chrome) window.chrome = {{}};
    if (!window.chrome.runtime) {{
        window.chrome.runtime = {{
            id: undefined,
            connect: function() {{ throw new Error('Invalid extension id: ""'); }},
            sendMessage: function() {{ throw new Error('Invalid extension id: ""'); }},
            getManifest: function() {{}},
            getURL: function(p) {{ return ''; }},
            PlatformOs: {{ANDROID:'android',CROS:'cros',LINUX:'linux',MAC:'mac',OPENBSD:'openbsd',WIN:'win'}},
        }};
    }}

    // --- 5. outerWidth/Height (headless = 0) ---
    if (window.outerWidth === 0) {{
        Object.defineProperty(window, 'outerWidth', {{get: () => window.innerWidth + 16}});
    }}
    if (window.outerHeight === 0) {{
        Object.defineProperty(window, 'outerHeight', {{get: () => window.innerHeight + 88}});
    }}

    // --- 6. connection rtt ---
    if (navigator.connection) {{
        Object.defineProperty(navigator.connection, 'rtt', {{get: () => {rtt}}});
    }}

    // --- 7. CDP variable cleanup ---
    (function() {{
        for (const p of Object.getOwnPropertyNames(window)) {{
            if (/^cdc_|^\\$cdc_/.test(p)) try {{ delete window[p]; }} catch(e) {{}}
        }}
        for (const p of Object.getOwnPropertyNames(document)) {{
            if (/^\\$cdc_|^\\$chrome_/.test(p)) try {{ delete document[p]; }} catch(e) {{}}
        }}
    }})();

    // --- 8. Canvas noise (stable per-profile seed) ---
    const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    CanvasRenderingContext2D.prototype.getImageData = function(...args) {{
        const d = _origGetImageData.apply(this, args);
        for (let i = 0; i < d.data.length; i += 4) {{
            if ((({seed} + i) * 9301 + 49297) % 233280 < 2332) d.data[i] ^= 1;
        }}
        return d;
    }};
    """
