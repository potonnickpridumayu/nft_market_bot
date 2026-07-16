"""Фаза B-2 — депозиты NFT от продавцов.

Фоновый поллер сканирует входящие NFT-трансферы на кошелёк-сейф (toncenter v3),
достаёт из forward_payload комментарий вида GS-<user_id>-XXXX, находит по нему
pending-intent продавца и кладёт подарок в его инвентарь (таблица gifts).

Переиспользует ton_client (адрес сейфа, HTTP-обвязка) — второго клиента нет.
"""
import asyncio
import base64
import json
import os
import re
import time
import logging
import urllib.request
import tg_gifts
import bot_updates

import ton_client
from pytoniq_core import Cell
from datetime import datetime, timezone

from db.queries import (
    add_gift,
    confirm_withdrawal,
    refund_stale_withdrawals,
    complete_deposit_intent,
    expire_stale_intents,
    get_pending_intent_by_code,
    is_event_processed,
    mark_event_processed,
    touch_unmatched_event,
    get_gift_by_nft_address,
    get_gift_by_tg_id,
    get_gift_by_identity,
    reclaim_tg_gift,
    set_gift_tg_id,
    get_or_create_user,
    transfer_gift,
    credit_ton_deposit,
    update_gift_meta,
    update_gift_tg_media,
)

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

GRACE_PERIOD = 30 * 60  # сек: сколько ждём матча несматченного трансфера
POLL_INTERVAL = 15   # секунд между проверками
BATCH_LIMIT = 20     # сколько последних трансферов смотрим за раз

# Брошенные заявки на депозит (pending, NFT так и не пришёл) гасим,
# чтобы они не висели вечно. Денег не держат — чистка гигиеническая.
INTENT_EXPIRE_HOURS = int(os.getenv("DEPOSIT_INTENT_EXPIRE_HOURS", "24"))
INTENT_SWEEP_INTERVAL = 3600  # раз в час достаточно
_last_intent_sweep = 0.0

DEPOSIT_PREFIX = "GS-DEP-"
MIN_DEPOSIT_TON = 0.05  # отсечка от пыли и случайных переводов

# Подарки, выведенные из сторонних маркетов, формально присылает их бот-банк,
# а не человек — такие депозиты сразу зачисляем реальному владельцу.
# Ключ: user_id формального отправителя; значение: user_id реального владельца.
TG_SENDER_ALIASES = {
    8184312603: 5762973275,  # "MRKT Bank" (бот mrkt) -> @twentop
}


def _gift_slug_from(name: str, number) -> str:
    """Собирает слаг t.me/nft из имени коллекции и номера — те же правила,
    что и gift_slug_from в api_server.py (дублируется, т.к. импорт оттуда
    сюда дал бы циклическую зависимость: api_server.py импортирует deposits.py)."""
    num = re.sub(r"[#\s]", "", str(number or ""))
    nm = re.sub(r"\s+", "", str(name or ""))
    return f"{nm}-{num}" if nm and num else ""


def decode_tx_comment(tx: dict) -> str | None:
    """Текстовый комментарий из обычного входящего TON-перевода."""
    in_msg = tx.get("in_msg") or {}
    content = in_msg.get("message_content") or {}
    decoded = content.get("decoded")
    if isinstance(decoded, dict):
        c = decoded.get("comment")
        if isinstance(c, str):
            return c.strip()
    body_b64 = content.get("body")
    if not body_b64:
        return None
    try:
        cell = Cell.one_from_boc(base64.b64decode(body_b64))
        cs = cell.begin_parse()
        if cs.load_uint(32) != 0:
            return None
        return cs.load_snake_string().strip()
    except Exception:
        return None


async def process_ton_deposits() -> None:
    """Один проход: входящие TON-переводы с кодом GS-DEP-<user_id> → баланс."""
    data = await ton_client._get(
        "/transactions",
        {
            "account": ton_client.TON_WALLET_ADDRESS,
            "limit": BATCH_LIMIT,
            "offset": 0,
            "sort": "desc",
        },
    )
    for tx in data.get("transactions") or []:
        # C-4: исходящие выводы подтверждаем всегда (идемпотентно), до дедупа
        await check_outgoing_withdrawals(tx)

        tx_hash = tx.get("hash") or ""
        if not tx_hash or await is_event_processed(tx_hash):
            continue

        in_msg = tx.get("in_msg") or {}
        value_nano = int(in_msg.get("value") or 0)
        comment = decode_tx_comment(tx)

        # не наш формат / нет комментария / пыль — молча пропускаем навсегда
        if not comment or not comment.startswith(DEPOSIT_PREFIX):
            await mark_event_processed(tx_hash, "not_deposit")
            continue

        amount_ton = value_nano / 1_000_000_000
        try:
            user_id = int(comment.removeprefix(DEPOSIT_PREFIX))
        except ValueError:
            await mark_event_processed(tx_hash, "bad_deposit_code")
            logger.warning("⛔ Битый код пополнения: %r (tx=%s)", comment, tx_hash)
            continue

        if amount_ton < MIN_DEPOSIT_TON:
            await mark_event_processed(tx_hash, "deposit_dust")
            logger.warning("⛔ Пополнение ниже минимума: %s TON от user %s", amount_ton, user_id)
            continue

        # атомарно: запись в ton_deposits + баланс + escrow_events одной транзакцией
        if await credit_ton_deposit(user_id, amount_ton, tx_hash):
            logger.info("💰 Пополнение: +%s TON → user %s (tx=%s)", amount_ton, user_id, tx_hash)

# ── C-4: подтверждение исходящих выводов TON ──────────────────────────────────

# Принимаем оба префикса: «GiftSafe» — выводы, отправленные до ребрендинга
# комментария на «ruby» (иначе зависшие в полёте выводы не подтвердятся).
WITHDRAWAL_RE = re.compile(r"(?:GiftSafe|ruby): withdrawal #(\d+)")
# Сколько ждём ончейн-подтверждения до возврата баланса. Защита от двойной
# выплаты при лаге индексатора toncenter: если вернуть слишком рано, а
# транзакция потом "проявится" — юзер получит и TON, и рефанд. Для тестов
# можно ужать через env (5 мин), для продакшена с реальными деньгами — 15.
REFUND_GRACE_MINUTES = int(os.getenv("REFUND_GRACE_MINUTES", "15"))


def decode_out_msg_comment(msg: dict) -> str | None:
    """Текстовый комментарий из исходящего сообщения транзакции."""
    content = msg.get("message_content") or {}
    decoded = content.get("decoded")
    if isinstance(decoded, dict):
        c = decoded.get("comment")
        if isinstance(c, str):
            return c.strip()
    body_b64 = content.get("body")
    if not body_b64:
        return None
    try:
        cell = Cell.one_from_boc(base64.b64decode(body_b64))
        cs = cell.begin_parse()
        if cs.load_uint(32) != 0:
            return None
        return cs.load_snake_string().strip()
    except Exception:
        return None


async def check_outgoing_withdrawals(tx: dict) -> None:
    """Ищем в исходящих сообщениях транзакции комментарии наших выводов.

    Идемпотентно: confirm_withdrawal переводит в confirmed только из
    pending/sent, повторный вызов — no-op."""
    for msg in tx.get("out_msgs") or []:
        comment = decode_out_msg_comment(msg)
        if not comment:
            continue
        m = WITHDRAWAL_RE.fullmatch(comment)
        if not m:
            continue
        wd_id = int(m.group(1))
        if await confirm_withdrawal(wd_id):
            logger.info("✅ Вывод #%s подтверждён ончейн (tx=%s)", wd_id, tx.get("hash"))


async def process_withdrawal_refunds() -> None:
    """Возврат баланса по выводам, зависшим без ончейн-подтверждения."""
    refunded = await refund_stale_withdrawals(REFUND_GRACE_MINUTES)
    for r in refunded:
        logger.warning(
            "↩️ Вывод #%s не подтвердился за %s мин — %s TON возвращено user %s",
            r["wd_id"], REFUND_GRACE_MINUTES, r["amount_ton"], r["user_id"],
        )



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
            existing = await get_gift_by_nft_address(nft_address)
            if existing:
                # Повторный депозит известного NFT — возвращаем гифт
                # владельцу вместо создания дубля.
                gift_id = existing["gift_id"]
                await transfer_gift(gift_id, intent["user_id"])
                # Если метаданные с прошлого раза пустые/заглушка —
                # пробуем дообогатить (best-effort, как и первый fetch).
                needs_meta = (
                    not existing.get("image_url")
                    or existing.get("gift_name") in ("", "NFT Gift", None)
                )
                if needs_meta:
                    meta = await fetch_nft_meta(nft_address)
                    new_name = meta.get("name") or existing.get("gift_name") or "NFT Gift"
                    new_image = meta.get("image") or existing.get("image_url") or ""
                    if meta.get("name") or meta.get("image"):
                        await update_gift_meta(gift_id, new_name, new_image)
                        logger.info(
                            "🖼 Метаданные гифта %s дообогащены: name=%r",
                            gift_id, new_name,
                        )
            else:
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


# ── Rubuy Bank: депозиты нативных Telegram-подарков ──────────────────────────

async def _notify_user(chat_id: int, text: str) -> None:
    """Best-effort уведомление в Telegram. Та же схема, что notify_seller
    в api_server.py (urllib + to_thread) — чтобы не тащить aiogram в поллер."""
    if not BOT_TOKEN:
        return

    def _send():
        data = json.dumps({"chat_id": chat_id, "text": text,
                           "parse_mode": "HTML"}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=data, headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()

    try:
        await asyncio.to_thread(_send)
    except Exception:
        pass


async def _notify_admins(text: str) -> None:
    admin_ids = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",")
                 if x.strip().isdigit()]
    for admin_id in admin_ids:
        await _notify_user(admin_id, text)


async def process_tg_gifts() -> None:
    """Один проход: новые уникальные Telegram-подарки на бизнес-аккаунте → gifts.

    В отличие от ончейн-депозита, тут нет кода/intent: атрибуция через
    sender_user, который Telegram прикладывает к каждому owned_gift."""
    if not await tg_gifts.is_configured():
        return

    for g in await tg_gifts.get_owned_unique_gifts():
        owned_gift_id = g.get("owned_gift_id") or g.get("id")
        if not owned_gift_id:
            continue

        gift_info = g.get("gift") or {}
        # base_name — чистое имя без номера ("Sakura Flower"); name дублирует
        # номер ("SakuraFlower-33824"), поэтому для отображения берём base_name
        collection_name = gift_info.get("base_name") or gift_info.get("name") or "Telegram Gift"
        gift_name = collection_name
        number = gift_info.get("number") or g.get("number")
        gift_number = str(number) if number else ""
        sticker = (gift_info.get("model") or {}).get("sticker") or {}
        tg_sticker = sticker.get("file_id") or ""
        tg_thumb = ((sticker.get("thumbnail") or {}).get("file_id")) or ""

        # Фон подарка: цвета градиента + стикер-узор + названия атрибутов
        # (модель/фон/символ — по ним работают фильтры маркета)
        backdrop_info = gift_info.get("backdrop") or {}
        colors = backdrop_info.get("colors") or {}
        symbol_info = gift_info.get("symbol") or {}
        symbol_thumb = ((symbol_info.get("sticker") or {})
                        .get("thumbnail") or {}).get("file_id") or ""
        tg_backdrop = ""
        if colors:
            tg_backdrop = json.dumps({
                "center": colors.get("center_color"),
                "edge": colors.get("edge_color"),
                "symbol": colors.get("symbol_color"),
                "pattern": symbol_thumb,
                "model_name": (gift_info.get("model") or {}).get("name") or "",
                "backdrop_name": backdrop_info.get("name") or "",
                "symbol_name": symbol_info.get("name") or "",
                # редкость атрибута в промилле (÷10 = проценты на фронте)
                "model_rarity": (gift_info.get("model") or {}).get("rarity_per_mille"),
                "backdrop_rarity": backdrop_info.get("rarity_per_mille"),
                "symbol_rarity": symbol_info.get("rarity_per_mille"),
            })

        existing = await get_gift_by_tg_id(owned_gift_id)
        if existing:
            # Дообогащение задним числом: чистое имя вместо "Name-123 123",
            # file_id стикеров, фон с узором и атрибутами
            if gift_name and (existing["gift_name"] != gift_name
                              or not existing["tg_sticker"]
                              or (tg_backdrop and existing["tg_backdrop"] != tg_backdrop)):
                await update_gift_tg_media(
                    existing["gift_id"], gift_name, gift_number,
                    tg_sticker, tg_thumb, tg_backdrop,
                )
                logger.info("🖼 TG-гифт %s дообогащён: %r #%s",
                            existing["gift_id"], gift_name, gift_number)
            continue

        # Имена полей сверяем по первым живым payload'ам — логируем целиком
        logger.info("🎁 Новый TG-подарок, raw: %s", json.dumps(g, ensure_ascii=False))

        # Тот же физический TG-гифт (коллекция+номер уникальны) уже есть у нас
        # под другим owned_gift_id — Telegram выдаёт новый owned_gift_id на
        # каждый ре-трансфер (вывод из Rubuy Bank и повторный занос), так что
        # lookup выше по owned_gift_id этого не ловит. Раньше это плодило
        # дубликат-строку, а старая зависала с уже неактуальным владельцем.
        dup = await get_gift_by_identity(gift_name, gift_number) if gift_number else None
        if dup:
            sender = g.get("sender_user")
            sender_id = sender["id"] if sender and sender.get("id") else None
            if sender_id in TG_SENDER_ALIASES:
                sender_id = TG_SENDER_ALIASES[sender_id]
                await get_or_create_user(sender_id, "", "")
            elif sender_id:
                full_name = " ".join(
                    p for p in [sender.get("first_name"), sender.get("last_name")] if p
                )
                await get_or_create_user(sender_id, sender.get("username", "") or "", full_name)
            await reclaim_tg_gift(
                dup["gift_id"], sender_id, owned_gift_id,
                gift_name, gift_number, tg_sticker, tg_thumb, tg_backdrop,
            )
            logger.info(
                "♻️ TG-гифт переоформлен (ре-депозит): %s #%s → user %s "
                "(gift_id=%s, было owned=%s → стало %s)",
                gift_name, gift_number, sender_id, dup["gift_id"],
                dup.get("tg_owned_gift_id"), owned_gift_id,
            )
            if sender_id:
                gift_slug = _gift_slug_from(gift_name, gift_number)
                link_line = (
                    f'\n🔗 <a href="https://t.me/nft/{gift_slug}">t.me/nft/{gift_slug}</a>'
                    if gift_slug else ""
                )
                await _notify_user(
                    sender_id,
                    f"🎁 <b>Подарок получен в ruby!</b>\n\n"
                    f"✨ {gift_name}{' #' + gift_number if gift_number else ''}\n"
                    f"Смотри в 💼 Портфеле — можно выставить на продажу "
                    f"или вернуть обратно в Telegram."
                    + link_line
                )
            else:
                await _notify_admins(
                    f"⚠️ <b>Анонимный ре-депозит в сейф ruby</b>\n\n"
                    f"🎁 {gift_name}{' #' + gift_number if gift_number else ''}\n"
                    f"🆔 gift_id: {dup['gift_id']}\n\n"
                    f"Отправитель скрыл личность — подарок помечен unclaimed."
                )
            continue

        sender = g.get("sender_user")
        if not sender or not sender.get("id"):
            # Отправитель скрыл личность — ценность не теряем: зачисляем
            # как unclaimed (owner NULL) и зовём админа привязать руками.
            gift_id = await add_gift(
                owner_id=None,
                collection_name=collection_name,
                gift_name=gift_name,
                gift_number=gift_number,
            )
            await set_gift_tg_id(gift_id, owned_gift_id)
            await update_gift_tg_media(gift_id, gift_name, gift_number,
                                   tg_sticker, tg_thumb, tg_backdrop)
            logger.warning(
                "🎁❓ Анонимный TG-подарок зачислен как unclaimed: gift_id=%s, owned=%s",
                gift_id, owned_gift_id,
            )
            await _notify_admins(
                f"⚠️ <b>Анонимный подарок в сейфе ruby</b>\n\n"
                f"🎁 {gift_name}{' #' + gift_number if gift_number else ''}\n"
                f"🆔 gift_id: {gift_id}\n\n"
                f"Отправитель скрыл личность — нужна ручная привязка к юзеру."
            )
            continue

        sender_id = sender["id"]
        if sender_id in TG_SENDER_ALIASES:
            sender_id = TG_SENDER_ALIASES[sender_id]
            await get_or_create_user(sender_id, "", "")
        else:
            full_name = " ".join(
                p for p in [sender.get("first_name"), sender.get("last_name")] if p
            )
            await get_or_create_user(sender_id, sender.get("username", "") or "", full_name)

        gift_id = await add_gift(
            owner_id=sender_id,
            collection_name=collection_name,
            gift_name=gift_name,
            gift_number=gift_number,
        )
        await set_gift_tg_id(gift_id, owned_gift_id)
        await update_gift_tg_media(gift_id, gift_name, gift_number,
                                   tg_sticker, tg_thumb, tg_backdrop)
        logger.info(
            "✅ TG-депозит: %s #%s → user %s (gift_id=%s, owned=%s)",
            gift_name, gift_number, sender_id, gift_id, owned_gift_id,
        )
        # Ссылка на подарок (t.me/nft/<slug>) — у Rubuy Bank подарков nft_address
        # пуст, слаг собираем из имени+номера, как и в остальных уведомлениях.
        gift_slug = _gift_slug_from(gift_name, gift_number)
        link_line = (
            f'\n🔗 <a href="https://t.me/nft/{gift_slug}">t.me/nft/{gift_slug}</a>'
            if gift_slug else ""
        )
        await _notify_user(
            sender_id,
            f"🎁 <b>Подарок получен в ruby!</b>\n\n"
            f"✨ {gift_name}{' #' + gift_number if gift_number else ''}\n"
            f"Смотри в 💼 Портфеле — можно выставить на продажу "
            f"или вернуть обратно в Telegram."
            + link_line
        )


async def sweep_stale_intents() -> None:
    """Гасит брошенные pending-заявки на депозит (старше INTENT_EXPIRE_HOURS).
    Троттлится до раза в час — это дешёвый UPDATE, но незачем гонять каждые 15с."""
    global _last_intent_sweep
    now = time.monotonic()
    if now - _last_intent_sweep < INTENT_SWEEP_INTERVAL:
        return
    _last_intent_sweep = now
    n = await expire_stale_intents(INTENT_EXPIRE_HOURS)
    if n:
        logger.info("🧹 Истекло брошенных заявок на депозит: %s", n)


async def poll_loop() -> None:
    """Бесконечный цикл поллера. Запускается из lifespan FastAPI."""
    logger.info("🔄 Поллер депозитов запущен (интервал %s сек)", POLL_INTERVAL)
    ton_steps = (
        ("nft_transfers", process_incoming_transfers),
        ("ton_deposits", process_ton_deposits),
        ("withdrawal_refunds", process_withdrawal_refunds),
    )
    # Rubuy Bank, апдейты бота и чистка заявок от TON-конфига не зависят
    tg_steps = (
        ("bot_updates", bot_updates.poll_bot_updates),
        ("tg_gifts", process_tg_gifts),
        ("expire_intents", sweep_stale_intents),
    )
    while True:
        try:
            steps = (ton_steps if ton_client.is_configured() else ()) + tg_steps
            # каждый шаг изолирован: сбой одного (например, 401 от
            # /nft/transfers) не блокирует депозиты TON и рефанды
            for step_name, step in steps:
                try:
                    await step()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(
                        "Поллер [%s]: %s: %s", step_name, e.__class__.__name__, e
                    )
        except asyncio.CancelledError:
            logger.info("Поллер депозитов остановлен")
            raise
        except Exception as e:
            # на всякий случай — не роняем цикл
            logger.warning("Поллер: %s: %s", e.__class__.__name__, e)
        await asyncio.sleep(POLL_INTERVAL)