"""Депозиты Telegram-подарков через Business-аккаунт (@twentop).

Бот подключён к бизнес-аккаунту с правами на подарки. Юзер шлёт уникальный
(upgraded) подарок аккаунту-сейфу как обычному человеку — без кодов.
Мы видим его через getBusinessAccountGifts (sender_user = кто прислал),
а отдаём через transferGift (Telegram берёт 25 Stars с баланса сейфа).

connection_id приходит из БД (business_connections, пишет bot_updates.py),
env TG_BUSINESS_CONNECTION_ID — запасной вариант для локальной отладки.
Модуль спит, пока подключения нет ни там, ни там.
"""
import os
import time
import logging

import aiohttp

from db.queries import get_active_business_connection

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
TRANSFER_STAR_COUNT = 25  # текущая такса Telegram за transferGift

_cached_conn_id: str = ""
_cache_ts: float = 0.0
_CACHE_TTL = 30  # сек — не долбим БД чаще, чем раз в поллер-тик


async def _resolve_connection_id() -> str:
    """БД — источник истины; env — fallback. Кеш, чтобы каждый Bot API
    вызов внутри одного тика не ходил в Postgres заново."""
    global _cached_conn_id, _cache_ts
    now = time.monotonic()
    if now - _cache_ts < _CACHE_TTL:
        return _cached_conn_id
    try:
        conn = await get_active_business_connection()
    except Exception:
        conn = None  # БД моргнула — не роняем, используем что было
    _cached_conn_id = ((conn or {}).get("connection_id")
                       or os.getenv("TG_BUSINESS_CONNECTION_ID", ""))
    _cache_ts = now
    return _cached_conn_id


async def is_configured() -> bool:
    return bool(BOT_TOKEN and await _resolve_connection_id())


async def _call(method: str, **params) -> dict | list:
    """Прямой вызов Bot API. Бросает RuntimeError с описанием при ok=false."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{API_BASE}/{method}", json=params,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json()
    if not data.get("ok"):
        raise RuntimeError(
            f"Bot API {method} -> {data.get('error_code')}: {data.get('description')}"
        )
    return data["result"]


async def get_owned_unique_gifts() -> list[dict]:
    """Все уникальные подарки на бизнес-аккаунте (с пагинацией)."""
    connection_id = await _resolve_connection_id()
    gifts: list[dict] = []
    offset = ""
    while True:
        result = await _call(
            "getBusinessAccountGifts",
            business_connection_id=connection_id,
            offset=offset,
            limit=100,
        )
        batch = result.get("gifts") or []
        gifts.extend(g for g in batch if g.get("type") == "unique")
        offset = result.get("next_offset") or ""
        if not offset or not batch:
            break
    return gifts


async def transfer_unique_gift(owned_gift_id: str, to_chat_id: int) -> bool:
    """Передать уникальный подарок юзеру (доставка покупателю / вывод).

    Telegram спишет TRANSFER_STAR_COUNT Stars с баланса бизнес-аккаунта."""
    return await _call(
        "transferGift",
        business_connection_id=await _resolve_connection_id(),
        owned_gift_id=owned_gift_id,
        new_owner_chat_id=to_chat_id,
        star_count=TRANSFER_STAR_COUNT,
    )