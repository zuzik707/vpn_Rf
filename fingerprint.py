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

    w, h = vp

    return f"""
    // --- Helpers ---
    const _nativeDef = (obj, prop, val) => {{
        Object.defineProperty(obj, prop, {{
            value: val, configurable: true, enumerable: true, writable: false
        }});
    }};
    const _nativeGetter = (obj, prop, val) => {{
        const fn = function() {{ return val; }};
        _spoofToString(fn, 'get ' + prop);
        Object.defineProperty(obj, prop, {{
            get: fn, configurable: true, enumerable: true
        }});
    }};
    const _spoofToString = (fn, name) => {{
        fn.toString = () => 'function ' + name + '() {{ [native code] }}';
        if (fn.toString.toString) fn.toString.toString = () => 'function toString() {{ [native code] }}';
    }};

    // --- 1. webdriver = false (real Chrome has false, not undefined) ---
    _nativeGetter(Navigator.prototype, 'webdriver', false);

    // --- 2. WebGL fingerprint (toString-safe) ---
    const _patchGL = (proto) => {{
        const orig = proto.getParameter;
        const patched = function getParameter(p) {{
            if (p === 37445) return '{vendor}';
            if (p === 37446) return '{renderer}';
            return orig.call(this, p);
        }};
        _spoofToString(patched, 'getParameter');
        proto.getParameter = patched;
    }};
    _patchGL(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') _patchGL(WebGL2RenderingContext.prototype);

    // --- 3. Hardware specs (getters, not values — CF checks descriptors) ---
    _nativeGetter(Navigator.prototype, 'hardwareConcurrency', {hc});
    _nativeGetter(Navigator.prototype, 'deviceMemory', {dm});

    // --- 4. navigator.languages ---
    Object.defineProperty(Navigator.prototype, 'languages', {{
        get: () => Object.freeze(['en-US', 'en', 'uz']),
        configurable: true, enumerable: true
    }});
    _nativeGetter(Navigator.prototype, 'language', 'en-US');

    // --- 5. navigator.plugins (real Chrome has PDF plugins) ---
    Object.defineProperty(Navigator.prototype, 'plugins', {{
        get: () => {{
            const arr = [
                {{name: 'PDF Viewer', filename: 'internal-pdf-viewer', description: 'Portable Document Format', length: 1}},
                {{name: 'Chrome PDF Viewer', filename: 'internal-pdf-viewer', description: '', length: 1}},
                {{name: 'Chromium PDF Viewer', filename: 'internal-pdf-viewer', description: '', length: 1}},
            ];
            arr.item = (i) => arr[i];
            arr.namedItem = (n) => arr.find(p => p.name === n);
            arr.refresh = () => {{}};
            return arr;
        }},
        configurable: true, enumerable: true
    }});

    // --- 6. chrome.runtime + chrome.app stubs ---
    if (!window.chrome) window.chrome = {{}};
    if (!window.chrome.runtime) {{
        window.chrome.runtime = {{
            connect: function() {{ throw new Error('Invalid extension id: ""'); }},
            sendMessage: function() {{ throw new Error('Invalid extension id: ""'); }},
            getManifest: function() {{}},
            getURL: function(p) {{ return ''; }},
            PlatformOs: {{ANDROID:'android',CROS:'cros',LINUX:'linux',MAC:'mac',OPENBSD:'openbsd',WIN:'win'}},
        }};
    }}
    if (!window.chrome.app) {{
        window.chrome.app = {{
            isInstalled: false,
            InstallState: {{DISABLED:'disabled',INSTALLED:'installed',NOT_INSTALLED:'not_installed'}},
            RunningState: {{CANNOT_RUN:'cannot_run',READY_TO_RUN:'ready_to_run',RUNNING:'running'}},
        }};
    }}

    // --- 6. outerWidth/Height + screen ---
    if (window.outerWidth === 0) {{
        Object.defineProperty(window, 'outerWidth', {{get: () => window.innerWidth + 16, configurable: true}});
    }}
    if (window.outerHeight === 0) {{
        Object.defineProperty(window, 'outerHeight', {{get: () => window.innerHeight + 88, configurable: true}});
    }}
    _nativeDef(window.screen, 'width', {w});
    _nativeDef(window.screen, 'height', {h});
    _nativeDef(window.screen, 'availWidth', {w});
    _nativeDef(window.screen, 'availHeight', {h} - 40);
    _nativeDef(window.screen, 'colorDepth', 24);

    // --- 7. connection rtt ---
    if (navigator.connection) {{
        _nativeDef(navigator.connection, 'rtt', {rtt});
    }}

    // --- 8. WebRTC IP leak protection (Proxy-based, preserves instanceof) ---
    if (window.RTCPeerConnection) {{
        const _origRTC = window.RTCPeerConnection;
        window.RTCPeerConnection = new Proxy(_origRTC, {{
            construct(target, args) {{
                if (args[0] && args[0].iceServers) args[0].iceServers = [];
                return new target(...args);
            }}
        }});
        window.RTCPeerConnection.prototype = _origRTC.prototype;
        _spoofToString(window.RTCPeerConnection, 'RTCPeerConnection');
        if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = window.RTCPeerConnection;
    }}

    // --- 9. CDP variable cleanup ---
    (function() {{
        for (const p of Object.getOwnPropertyNames(window)) {{
            if (/^cdc_|^\\$cdc_/.test(p)) try {{ delete window[p]; }} catch(e) {{}}
        }}
        for (const p of Object.getOwnPropertyNames(document)) {{
            if (/^\\$cdc_|^\\$chrome_/.test(p)) try {{ delete document[p]; }} catch(e) {{}}
        }}
    }})();

    // --- 10. Canvas noise (skips Turnstile challenge canvases) ---
    const _seed = {seed};
    function _addCanvasNoise(data) {{
        for (let i = 0; i < data.length; i += 4) {{
            if (((_seed + i) * 9301 + 49297) % 233280 < 2332) data[i] ^= 1;
        }}
    }}
    function _isChallengeCanvas(canvas) {{
        try {{
            if (!canvas || !canvas.closest) return false;
            return !!(canvas.closest('.cf-turnstile, [data-challenge], iframe'));
        }} catch(e) {{ return false; }}
    }}
    const _origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    const _patchedGetImageData = function getImageData(...args) {{
        const d = _origGetImageData.apply(this, args);
        if (!_isChallengeCanvas(this.canvas)) _addCanvasNoise(d.data);
        return d;
    }};
    _spoofToString(_patchedGetImageData, 'getImageData');
    CanvasRenderingContext2D.prototype.getImageData = _patchedGetImageData;

    const _origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const _patchedToDataURL = function toDataURL(...args) {{
        if (!_isChallengeCanvas(this)) {{
            try {{
                const ctx = this.getContext('2d');
                if (ctx) {{
                    const d = _origGetImageData.call(ctx, 0, 0, this.width, this.height);
                    _addCanvasNoise(d.data);
                    ctx.putImageData(d, 0, 0);
                }}
            }} catch(e) {{}}
        }}
        return _origToDataURL.apply(this, args);
    }};
    _spoofToString(_patchedToDataURL, 'toDataURL');
    HTMLCanvasElement.prototype.toDataURL = _patchedToDataURL;

    const _origToBlob = HTMLCanvasElement.prototype.toBlob;
    const _patchedToBlob = function toBlob(cb, ...args) {{
        if (!_isChallengeCanvas(this)) {{
            try {{
                const ctx = this.getContext('2d');
                if (ctx) {{
                    const d = _origGetImageData.call(ctx, 0, 0, this.width, this.height);
                    _addCanvasNoise(d.data);
                    ctx.putImageData(d, 0, 0);
                }}
            }} catch(e) {{}}
        }}
        return _origToBlob.call(this, cb, ...args);
    }};
    _spoofToString(_patchedToBlob, 'toBlob');
    HTMLCanvasElement.prototype.toBlob = _patchedToBlob;

    // --- 11. AudioContext fingerprint noise ---
    if (typeof AnalyserNode !== 'undefined') {{
        const _origGetFloatFreq = AnalyserNode.prototype.getFloatFrequencyData;
        const _patchedFloat = function getFloatFrequencyData(arr) {{
            _origGetFloatFreq.call(this, arr);
            for (let i = 0; i < arr.length; i += 7) arr[i] += 0.001 * ((_seed + i) % 3 - 1);
        }};
        _spoofToString(_patchedFloat, 'getFloatFrequencyData');
        AnalyserNode.prototype.getFloatFrequencyData = _patchedFloat;
    }}
    """
