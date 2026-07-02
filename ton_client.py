"""Фаза B-1 — чтение блокчейна TON (только чтение, без ключей).

Модуль отвечает за одно: по адресу кошелька-сейфа площадки прочитать
его баланс (в GRAM/TON) и список NFT-подарков, которые на нём лежат.
Приватный ключ здесь НЕ используется — читать блокчейн можно по публичному адресу.

Работает через TON API (toncenter). Сеть и ключ берутся из переменных окружения:
    TON_NETWORK        = "testnet" | "mainnet"   (по умолчанию testnet)
    TON_API_KEY        = ключ toncenter (опционально; без него лимит 1 req/sec)
    TON_WALLET_ADDRESS = адрес кошелька-сейфа (публичный)
"""
import os
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

TON_NETWORK = os.getenv("TON_NETWORK", "testnet").lower()
TON_API_KEY = os.getenv("TON_API_KEY", "")
TON_WALLET_ADDRESS = os.getenv("TON_WALLET_ADDRESS", "")

# База TON API (toncenter). Для testnet и mainnet — разные хосты.
_BASE = (
    "https://testnet.toncenter.com/api/v3"
    if TON_NETWORK == "testnet"
    else "https://toncenter.com/api/v3"
)

# 1 GRAM/TON = 10^9 нанотонов
NANO = 1_000_000_000


def _headers() -> dict:
    h = {"Accept": "application/json"}
    if TON_API_KEY:
        h["X-API-Key"] = TON_API_KEY
    return h


async def _get(path: str, params: dict) -> dict:
    """GET-запрос к TON API с обработкой ошибок. Возвращает распарсенный JSON."""
    url = f"{_BASE}{path}"
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, params=params, headers=_headers()) as resp:
            text = await resp.text()
            if resp.status != 200:
                logger.warning("TON API %s -> %s: %s", path, resp.status, text[:300])
                raise RuntimeError(f"TON API {resp.status}: {text[:200]}")
            return await resp.json()


def is_configured() -> bool:
    """Готов ли модуль к работе — задан ли адрес кошелька-сейфа."""
    return bool(TON_WALLET_ADDRESS)


async def get_wallet_balance(address: Optional[str] = None) -> float:
    """Баланс кошелька в GRAM/TON (float)."""
    addr = address or TON_WALLET_ADDRESS
    if not addr:
        raise RuntimeError("TON_WALLET_ADDRESS не задан")
    data = await _get("/account", {"address": addr})
    balance_nano = int(data.get("balance", 0) or 0)
    return balance_nano / NANO


async def get_wallet_nfts(address: Optional[str] = None, limit: int = 100) -> list:
    """Список NFT-подарков, которыми владеет адрес.

    Возвращает список словарей с полями, удобными для фронта:
        nft_address, index, collection_address, name, image, raw_metadata
    """
    addr = address or TON_WALLET_ADDRESS
    if not addr:
        raise RuntimeError("TON_WALLET_ADDRESS не задан")

    data = await _get("/nft/items", {"owner_address": addr, "limit": limit, "offset": 0})
    items = data.get("nft_items", []) or []

    result = []
    for it in items:
        meta = (it.get("content") or {}).get("_") or {}
        # у toncenter метаданные могут лежать по-разному — аккуратно достаём
        content = it.get("content") or {}
        name = (
            content.get("name")
            or meta.get("name")
            or ""
        )
        image = (
            content.get("image")
            or meta.get("image")
            or ""
        )
        result.append({
            "nft_address": it.get("address", ""),
            "index": it.get("index"),
            "collection_address": it.get("collection_address", ""),
            "name": name,
            "image": image,
            "raw_metadata": content,
        })
    return result


async def get_escrow_snapshot() -> dict:
    """Сводка по кошельку-сейфу: сеть, адрес, баланс и подарки на нём.
    Используется эндпоинтом /api/escrow/status для проверки, что связь с TON есть."""
    if not is_configured():
        return {
            "configured": False,
            "network": TON_NETWORK,
            "message": "TON_WALLET_ADDRESS не задан в переменных окружения",
        }
    balance = await get_wallet_balance()
    nfts = await get_wallet_nfts()
    return {
        "configured": True,
        "network": TON_NETWORK,
        "address": TON_WALLET_ADDRESS,
        "balance": balance,
        "nft_count": len(nfts),
        "nfts": nfts,
    }