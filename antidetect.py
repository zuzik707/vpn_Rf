"""
Anti-detection модуль.

Закрывает все известные точки детекта VFS Global / Cloudflare:
1. navigator.webdriver — nodriver уже патчит, но делаем double-check
2. Chrome DevTools Protocol (CDP) leak
3. WebGL fingerprint consistency
4. Canvas fingerprint
5. Поведенческий анализ (timing, mouse, typing)
6. Timezone / locale consistency
7. Screen resolution consistency
8. Headless-специфичные утечки
"""

import logging
import random

logger = logging.getLogger(__name__)

# JS-скрипты для инъекции при старте страницы
STEALTH_SCRIPTS = [
    # 1. Убираем все следы webdriver
    """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    delete navigator.__proto__.webdriver;
    """,

    # 2. Подменяем navigator.plugins (у headless их нет)
    """
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const plugins = [
                {name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format'},
                {name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: ''},
                {name: 'Native Client', filename: 'internal-nacl-plugin', description: ''},
            ];
            plugins.length = 3;
            return plugins;
        }
    });
    """,

    # 3. Подменяем navigator.languages
    """
    Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en', 'uz']});
    Object.defineProperty(navigator, 'language', {get: () => 'en-US'});
    """,

    # 4. Фиксим chrome.runtime (отсутствие = детект)
    """
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) window.chrome.runtime = {id: undefined};
    """,

    # 5. Подменяем permissions query (headless отдаёт 'denied' на notifications)
    """
    const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
    window.navigator.permissions.query = (params) => {
        if (params.name === 'notifications') {
            return Promise.resolve({state: Notification.permission});
        }
        return origQuery(params);
    };
    """,

    # 6. Закрываем утечку через window.outerWidth/outerHeight
    """
    if (window.outerWidth === 0) {
        Object.defineProperty(window, 'outerWidth', {get: () => window.innerWidth + 16});
    }
    if (window.outerHeight === 0) {
        Object.defineProperty(window, 'outerHeight', {get: () => window.innerHeight + 88});
    }
    """,

    # 7. WebGL vendor/renderer — должны быть как у реального Chrome
    """
    const getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Google Inc. (NVIDIA)';
        if (param === 37446) return 'ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 6GB Direct3D11 vs_5_0 ps_5_0, D3D11)';
        return getParam.call(this, param);
    };
    """,

    # 8. Предотвращаем детект через Error stack trace (automation frameworks оставляют следы)
    """
    const origError = Error;
    Error = function(...args) {
        const err = new origError(...args);
        const stack = err.stack;
        if (stack) {
            err.stack = stack.split('\\n').filter(line =>
                !line.includes('puppeteer') &&
                !line.includes('playwright') &&
                !line.includes('selenium') &&
                !line.includes('webdriver') &&
                !line.includes('nodriver')
            ).join('\\n');
        }
        return err;
    };
    Error.prototype = origError.prototype;
    """,

    # 9. Подменяем connection rtt (headless часто 0)
    """
    if (navigator.connection) {
        Object.defineProperty(navigator.connection, 'rtt', {get: () => 50});
    }
    """,

    # 10. Закрываем MediaDevices leak
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
]


async def inject_stealth(page) -> None:
    """Инжектим все stealth-скрипты в страницу."""
    for i, script in enumerate(STEALTH_SCRIPTS):
        try:
            await page.evaluate(script)
        except Exception as e:
            logger.debug("Stealth script %d warning: %s", i, e)
    logger.info("Stealth скрипты инжектированы (%d шт.)", len(STEALTH_SCRIPTS))


async def setup_stealth_on_new_page(page) -> None:
    """
    Настраиваем stealth для новой страницы.
    Вызывается ПЕРЕД навигацией.
    """
    try:
        import nodriver.cdp.page as cdp_page
        combined_script = "\n".join(STEALTH_SCRIPTS)
        await page.send(cdp_page.add_script_to_evaluate_on_new_document(source=combined_script))
        logger.info("Stealth скрипты добавлены для всех будущих страниц")
    except Exception as e:
        logger.warning("CDP addScriptToEvaluateOnNewDocument failed: %s — fallback to post-inject", e)


def get_realistic_viewport() -> tuple[int, int]:
    """Возвращает реалистичное разрешение экрана."""
    resolutions = [
        (1920, 1080),
        (1366, 768),
        (1536, 864),
        (1440, 900),
        (1280, 720),
    ]
    return random.choice(resolutions)


def get_chrome_args() -> list[str]:
    """Аргументы запуска Chrome для минимизации детекта."""
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


