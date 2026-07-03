"""Фаза B-2 — депозиты NFT от продавцов.

Фоновый поллер сканирует входящие NFT-трансферы на кошелёк-сейф (toncenter v3),
достаёт из forward_payload комментарий вида GS-<user_id>-XXXX, находит по нему
pending-intent продавца и кладёт подарок в его инвентарь (таблица gifts).

Переиспользует ton_client (адрес сейфа, HTTP-обвязка) — второго клиента нет.
"""
import asyncio
import base64
import logging

import ton_client
from pytoniq_core import Cell
from datetime import datetime, timezone

from db.queries import (
    add_gift,
    complete_deposit_intent,
    get_pending_intent_by_code,
    is_event_processed,
    mark_event_processed,
    touch_unmatched_event
)

logger = logging.getLogger(__name__)

GRACE_PERIOD = 30 * 60  # сек: сколько ждём матча несматченного трансфера
POLL_INTERVAL = 15   # секунд между проверками
BATCH_LIMIT = 20     # сколько последних трансферов смотрим за раз


def decode_comment(transfer: dict) -> str | None:
    """Достаём текстовый комментарий из NFT-трансфера.

    Ступень 1: toncenter иногда декодирует сам (decoded_forward_payload).
    Ступень 2: парсим base64-BOC из forward_payload (op=0 + snake string).
    Любая ошибка/не-текст → None, поллер просто идёт дальше."""
    decoded = transfer.get("decoded_forward_payload")
    if decoded:
        if isinstance(decoded, str):
            return decoded.strip()
        if isinstance(decoded, dict):
            for key in ("comment", "text", "value"):
                if isinstance(decoded.get(key), str):
                    return decoded[key].strip()

    payload_b64 = transfer.get("forward_payload")
    if not payload_b64:
        return None
    try:
        cell = Cell.one_from_boc(base64.b64decode(payload_b64))
        cs = cell.begin_parse()
        if cs.load_uint(32) != 0:  # 0x00000000 = текстовый комментарий
            return None
        return cs.load_snake_string().strip()
    except Exception:
        return None


async def fetch_nft_meta(nft_address: str) -> dict:
    """Имя/картинка/коллекция конкретного NFT. Best-effort."""
    try:
        data = await ton_client._get(
            "/nft/items", {"address": nft_address, "limit": 1, "offset": 0}
        )
        items = data.get("nft_items") or []
        if not items:
            return {}
        it = items[0]
        content = it.get("content") or {}
        return {
            "name": content.get("name") or "",
            "image": content.get("image") or "",
            "collection_address": it.get("collection_address") or "",
        }
    except Exception:
        return {}


async def process_incoming_transfers() -> None:
    """Один проход: берём свежие входящие NFT-трансферы и матчим по коду."""
    data = await ton_client._get(
        "/nft/transfers",
        {
            "owner_address": ton_client.TON_WALLET_ADDRESS,
            "direction": "in",
            "limit": BATCH_LIMIT,
            "offset": 0,
        },
    )
    transfers = data.get("nft_transfers") or []

    for t in transfers:
        tx_hash = t.get("transaction_hash") or ""
        if not tx_hash or await is_event_processed(tx_hash):
            continue

        nft_address = t.get("nft_address") or ""
        code = decode_comment(t)
        intent = await get_pending_intent_by_code(code) if code else None

        if intent:
            meta = await fetch_nft_meta(nft_address)
            gift_id = await add_gift(
                owner_id=intent["user_id"],
                collection_name=meta.get("collection_address", "") or "TON NFT",
                gift_name=meta.get("name") or "NFT Gift",
                gift_number="",
                rarity="Common",
                image_url=meta.get("image", ""),
                nft_address=nft_address,
            )
            await complete_deposit_intent(
                intent["intent_id"], nft_address, gift_id,
                from_address=t.get("old_owner") or "",
            )
            await mark_event_processed(tx_hash, "completed")
            logger.info(
                "✅ Депозит: NFT %s → user %s (gift_id=%s, code=%s)",
                nft_address, intent["user_id"], gift_id, code,
            )
        else:
            # Матча нет: либо NFT реально без кода, либо toncenter ещё не
            # проиндексировал forward_payload. Даём grace-период, потом финалим.
            first_seen = await touch_unmatched_event(tx_hash)
            age = (datetime.now(timezone.utc) - first_seen).total_seconds()
            if age >= GRACE_PERIOD:
                logger.warning(
                    "⛔ Несматченный NFT финализирован (лежит на сейфе бесхозным): "
                    "nft=%s, comment=%r, tx=%s",
                    nft_address, code, tx_hash,
                )
                await mark_event_processed(tx_hash, "unmatched_final")
            else:
                logger.info(
                    "⏳ Несматченный трансфер, ждём (%d/%d сек): tx=%s, comment=%r",
                    age, GRACE_PERIOD, tx_hash, code,
                )


async def poll_loop() -> None:
    """Бесконечный цикл поллера. Запускается из lifespan FastAPI."""
    logger.info("🔄 Поллер депозитов запущен (интервал %s сек)", POLL_INTERVAL)
    while True:
        try:
            if ton_client.is_configured():
                await process_incoming_transfers()
        except asyncio.CancelledError:
            logger.info("Поллер депозитов остановлен")
            raise
        except Exception as e:
            # сеть/рейт-лимит toncenter — не роняем цикл
            logger.warning("Поллер: %s: %s", e.__class__.__name__, e)
        await asyncio.sleep(POLL_INTERVAL)