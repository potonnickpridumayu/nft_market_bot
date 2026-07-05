"""Депозиты Telegram-подарков через Business-аккаунт (@twentop).

Бот подключён к бизнес-аккаунту с правами на подарки. Юзер шлёт уникальный
(upgraded) подарок аккаунту-сейфу как обычному человеку — без кодов.
Мы видим его через getBusinessAccountGifts (sender_user = кто прислал),
а отдаём через transferGift (Telegram берёт 25 Stars с баланса сейфа).

Модуль спит, пока в env нет TG_BUSINESS_CONNECTION_ID.
"""
import os
import logging

import aiohttp

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BUSINESS_CONNECTION_ID = os.getenv("TG_BUSINESS_CONNECTION_ID", "")

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
TRANSFER_STAR_COUNT = 25  # текущая такса Telegram за transferGift


def is_configured() -> bool:
    return bool(BOT_TOKEN and BUSINESS_CONNECTION_ID)


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
    gifts: list[dict] = []
    offset = ""
    while True:
        result = await _call(
            "getBusinessAccountGifts",
            business_connection_id=BUSINESS_CONNECTION_ID,
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
        business_connection_id=BUSINESS_CONNECTION_ID,
        owned_gift_id=owned_gift_id,
        new_owner_chat_id=to_chat_id,
        star_count=TRANSFER_STAR_COUNT,
    )