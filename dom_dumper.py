"""
DOM dumper — сохраняет HTML текущей страницы для отладки.
Используется при ошибках для анализа что именно видит бот.
"""

import logging
import os
import time

from config import Config

logger = logging.getLogger(__name__)

DUMP_DIR = os.path.join(os.path.dirname(__file__), "dom_dumps")


async def dump_page(page, tag: str = "debug") -> str | None:
    """Сохраняет HTML и возвращает путь к файлу."""
    os.makedirs(DUMP_DIR, exist_ok=True)
    try:
        html = await page.evaluate("document.documentElement.outerHTML")
        url = await page.evaluate("window.location.href")
    except Exception as e:
        logger.warning("DOM dump failed: %s", e)
        return None

    ts = int(time.time())
    path = os.path.join(DUMP_DIR, f"{tag}_{ts}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"<!-- URL: {url} -->\n<!-- Tag: {tag} -->\n<!-- Time: {ts} -->\n")
        f.write(html)

    logger.info("DOM dump: %s (%d bytes)", path, len(html))
    return path


def cleanup_dumps(max_age_hours: int = 24) -> int:
    """Удаляет старые дампы."""
    if not os.path.isdir(DUMP_DIR):
        return 0
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for f in os.listdir(DUMP_DIR):
        fp = os.path.join(DUMP_DIR, f)
        try:
            if os.path.getmtime(fp) < cutoff:
                os.remove(fp)
                removed += 1
        except OSError:
            pass
    return removed
