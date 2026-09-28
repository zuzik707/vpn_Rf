"""
Anti-detection модуль v2.

Закрывает векторы детекта VFS Global / Cloudflare:
1. navigator.webdriver
2. WebGL fingerprint (Linux-consistent)
3. navigator.plugins (Chrome 120+ Linux)
4. chrome.runtime realistic stub
5. Permissions, outerWidth/Height, connection rtt
6. MediaDevices, hardwareConcurrency, deviceMemory
7. CDP leak variable cleanup
"""

import logging
import random

logger = logging.getLogger(__name__)

STEALTH_SCRIPTS = [
    # 1. webdriver — nodriver already patches, double-check
    """
    Object.defineProperty(navigator, 'webdriver', {
        get: () => undefined,
        configurable: true
    });
    delete navigator.__proto__.webdriver;
    """,

    # 2. navigator.plugins — Chrome 120+ on Linux (NaCl removed since Chrome 117)
    """
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const pd = {0: {type: 'application/x-google-chrome-pdf', suffixes: 'pdf', description: 'Portable Document Format', enabledPlugin: null}};
            const p1 = Object.create(Plugin.prototype, {
                name: {value: 'Chrome PDF Plugin'},
                filename: {value: 'internal-pdf-viewer'},
                description: {value: 'Portable Document Format'},
                length: {value: 1},
                0: {value: pd[0]}
            });
            const p2 = Object.create(Plugin.prototype, {
                name: {value: 'Chrome PDF Viewer'},
                filename: {value: 'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
                description: {value: ''},
                length: {value: 1},
                0: {value: pd[0]}
            });
            const arr = [p1, p2];
            arr.item = (i) => arr[i] || null;
            arr.namedItem = (n) => arr.find(p => p.name === n) || null;
            arr.refresh = () => {};
            Object.setPrototypeOf(arr, PluginArray.prototype);
            return arr;
        }
    });
    """,

    # 3. navigator.languages
    """
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en', 'uz']});
    Object.defineProperty(navigator, 'language', {get: () => 'en-US'});
    """,

    # 4. chrome.runtime — realistic stub matching real Chrome
    """
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) {
        window.chrome.runtime = {
            id: undefined,
            connect: function() { throw new Error('Invalid extension id: ""'); },
            sendMessage: function() { throw new Error('Invalid extension id: ""'); },
            getManifest: function() {},
            getURL: function(path) { return ''; },
            OnInstalledReason: {CHROME_UPDATE:'chrome_update',INSTALL:'install',SHARED_MODULE_UPDATE:'shared_module_update',UPDATE:'update'},
            OnRestartRequiredReason: {APP_UPDATE:'app_update',OS_UPDATE:'os_update',PERIODIC:'periodic'},
            PlatformArch: {ARM:'arm',ARM64:'arm64',MIPS:'mips',MIPS64:'mips64',X86_32:'x86-32',X86_64:'x86-64'},
            PlatformOs: {ANDROID:'android',CROS:'cros',LINUX:'linux',MAC:'mac',OPENBSD:'openbsd',WIN:'win'},
            RequestUpdateCheckStatus: {NO_UPDATE:'no_update',THROTTLED:'throttled',UPDATE_AVAILABLE:'update_available'},
        };
    }
    """,

    # 5. Permissions query
    """
    const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
    window.navigator.permissions.query = (params) => {
        if (params.name === 'notifications') {
            return Promise.resolve({state: Notification.permission});
        }
        return origQuery(params);
    };
    """,

    # 6. outerWidth/outerHeight
    """
    if (window.outerWidth === 0) {
        Object.defineProperty(window, 'outerWidth', {get: () => window.innerWidth + 16});
    }
    if (window.outerHeight === 0) {
        Object.defineProperty(window, 'outerHeight', {get: () => window.innerHeight + 88});
    }
    """,

    # 7. WebGL — Linux-consistent renderer (BOTH WebGL1 AND WebGL2)
    """
    const webglVendor = 'Google Inc. (NVIDIA Corporation)';
    const webglRenderer = 'ANGLE (NVIDIA Corporation, NVIDIA GeForce GTX 1060 6GB/PCIe/SSE2, OpenGL 4.5.0)';
    const patchGetParam = (proto) => {
        const orig = proto.getParameter;
        proto.getParameter = function(param) {
            if (param === 37445) return webglVendor;
            if (param === 37446) return webglRenderer;
            return orig.call(this, param);
        };
    };
    patchGetParam(WebGLRenderingContext.prototype);
    if (typeof WebGL2RenderingContext !== 'undefined') {
        patchGetParam(WebGL2RenderingContext.prototype);
    }
    """,

    # 8. Connection rtt
    """
    if (navigator.connection) {
        Object.defineProperty(navigator.connection, 'rtt', {get: () => 50});
    }
    """,

    # 9. MediaDevices
    """
    if (navigator.mediaDevices) {
        const origEnum = navigator.mediaDevices.enumerateDevices;
        navigator.mediaDevices.enumerateDevices = async function() {
            const devices = await origEnum.call(this);
            if (devices.length === 0) {
                return [{
                    deviceId: 'default',
                    kind: 'audioinput',
                    label: '',
                    groupId: 'default'
                }];
            }
            return devices;
        };
    }
    """,

    # 10. hardwareConcurrency + deviceMemory — match claimed GPU
    """
    Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
    Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});
    """,

    # 11. CDP leak variable cleanup
    """
    (function() {
        const props = Object.getOwnPropertyNames(window);
        for (const p of props) {
            if (p.match(/^cdc_|^\\$cdc_/) || p.includes('_Array') || p.includes('_Proxy')) {
                try { delete window[p]; } catch(e) {}
            }
        }
        const docProps = Object.getOwnPropertyNames(document);
        for (const p of docProps) {
            if (p.match(/^\\$cdc_|^\\$chrome_/)) {
                try { delete document[p]; } catch(e) {}
            }
        }
    })();
    """,

    # 12. Canvas fingerprint stability — add minimal noise
    """
    const origToDataURL = HTMLCanvasElement.prototype.toDataURL;
    const origToBlob = HTMLCanvasElement.prototype.toBlob;
    const origGetImageData = CanvasRenderingContext2D.prototype.getImageData;
    const noise = 0.01;
    const seed = Math.floor(Math.random() * 1000);
    function addNoise(data) {
        for (let i = 0; i < data.length; i += 4) {
            const n = ((seed + i) * 9301 + 49297) % 233280;
            if (n < 233280 * noise) {
                data[i] = data[i] ^ 1;
            }
        }
    }
    CanvasRenderingContext2D.prototype.getImageData = function(...args) {
        const imageData = origGetImageData.apply(this, args);
        addNoise(imageData.data);
        return imageData;
    };
    """,
]


async def inject_stealth(page) -> None:
    """Post-navigation stealth — only for pages that missed addScriptToEvaluateOnNewDocument."""
    for i, script in enumerate(STEALTH_SCRIPTS):
        if not isinstance(script, str) or not script.strip():
            continue
        try:
            await page.evaluate(script)
        except Exception as e:
            logger.debug("Stealth script %d warning: %s", i, e)
    logger.info("Stealth скрипты инжектированы (%d шт.)", len(STEALTH_SCRIPTS))


async def setup_stealth_on_new_page(page) -> None:
    """Before navigation — injects via CDP for all future page loads."""
    try:
        import nodriver.cdp.page as cdp_page
        scripts = [s for s in STEALTH_SCRIPTS if isinstance(s, str) and s.strip()]
        combined_script = "\n".join(scripts)
        await page.send(cdp_page.add_script_to_evaluate_on_new_document(source=combined_script))
        logger.info("Stealth скрипты добавлены для всех будущих страниц")
    except Exception as e:
        logger.warning("CDP addScriptToEvaluateOnNewDocument failed: %s — fallback to post-inject", e)


def get_realistic_viewport() -> tuple[int, int]:
    """Fixed viewport per session — не менять между сессиями с одним user_data_dir."""
    resolutions = [
        (1920, 1080),
        (1366, 768),
        (1536, 864),
        (1440, 900),
        (1280, 720),
    ]
    return random.choice(resolutions)


def get_chrome_args() -> list[str]:
    """Chrome args for anti-detection."""
    width, height = get_realistic_viewport()
    return [
        f"--window-size={width},{height}",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--disable-infobars",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--lang=en-US",
    ]
