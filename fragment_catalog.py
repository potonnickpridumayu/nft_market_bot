"""Каталог атрибутов коллекции подарков со страницы Fragment.

fragment.com/gifts/<slug> отдаёт server-rendered HTML со списком всех
вариантов Model / Backdrop / Symbol коллекции: у каждого имя, картинка и
число заминченных (count). Из count считаем долю ≈ редкость. Авторизация
не нужна — обычный GET. Данные кэшируются в БД (gift_catalog), т.к. меняются
медленно (новые минты).

Никаких новых зависимостей: aiohttp уже в проекте, парсинг — регуляркой.
"""
import re
import logging

import aiohttp

logger = logging.getLogger(__name__)

FRAGMENT_BASE = "https://fragment.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Один элемент фильтра: имя (data-value) + картинка (/file/gifts/<slug>/<kind>.<hash>.<ext>) + count
_ITEM_RE = re.compile(
    r'js-attribute-item"\s+data-keywords="[^"]*"\s+data-value="([^"]*)".*?'
    r'<img src="(/file/gifts/[^"]+?\.(?:webp|svg|png|jpg))".*?'
    r'tm-main-filters-count">([\d,]+)</div>',
    re.DOTALL,
)

# kind в имени файла картинки: model. / backdrop. / symbol.
_KINDS = {"model": "models", "backdrop": "backdrops", "symbol": "symbols"}


def slugify(name: str) -> str:
    """Имя коллекции → слаг Fragment: только буквы/цифры, нижний регистр.
    «Light Sword»→lightsword, «Tama Gadget»→tamagadget, «Durov's Cap»→durovscap."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def parse_catalog(html: str) -> dict:
    """HTML страницы коллекции → {models, backdrops, symbols}.
    Каждый вариант: {name, count, pct, img}. Fragment рендерит список дважды —
    дедуплицируем по (kind, name). Отсортировано по возрастанию редкости."""
    buckets = {"models": {}, "backdrops": {}, "symbols": {}}
    for m in _ITEM_RE.finditer(html):
        name = m.group(1).strip()
        src = m.group(2)
        count = int(m.group(3).replace(",", ""))
        fname = src.rsplit("/", 1)[1]
        kind_key = _KINDS.get(fname.split(".", 1)[0])
        if not kind_key or not name or name in buckets[kind_key]:
            continue
        buckets[kind_key][name] = {"name": name, "count": count, "img": FRAGMENT_BASE + src}

    out = {}
    for key, items in buckets.items():
        total = sum(x["count"] for x in items.values()) or 1
        rows = list(items.values())
        for x in rows:
            x["pct"] = round(x["count"] / total * 100, 2)
        rows.sort(key=lambda z: z["count"])  # реже (меньше) — выше
        out[key] = rows
    return out


async def fetch_catalog(slug: str) -> dict | None:
    """Тянет и парсит каталог коллекции. None — если страницы нет/ошибка."""
    slug = slugify(slug)
    if not slug:
        return None
    url = f"{FRAGMENT_BASE}/gifts/{slug}"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": UA}) as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    logger.info("fragment catalog %s -> HTTP %s", slug, resp.status)
                    return None
                html = await resp.text()
    except Exception as e:
        logger.warning("fragment catalog fetch failed for %s: %s", slug, e)
        return None
    data = parse_catalog(html)
    # пустой результат (не та страница / вёрстка сменилась) — не кэшируем как успех
    if not any(data.get(k) for k in ("models", "backdrops", "symbols")):
        return None
    return data
