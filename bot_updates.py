"""Поллер апдейтов бота — замена легаси bot.py в живом процессе.

Живая система (api_server + deposits) раньше вообще не слушала getUpdates:
вебхука нет, bot.py не задеплоен (в нём локальный прокси). Из-за этого
/start ref_<id> не записывал рефералов, а business_connection апдейты
висели в очереди. Этот модуль — минимальный потребитель ровно двух типов:

  business_connection → business_connections в БД (Rubuy Bank)
  message /start [ref_<id>] → регистрация юзера + приветствие

Шаг в poll_loop() deposits.py, короткий poll без long-poll таймаута.
ВАЖНО: единственный getUpdates-потребитель. Запуск bot.py параллельно
(локально или где-то ещё) даст 409 Conflict — не делать так.
"""
import os
import logging

import aiohttp

from db.queries import get_or_create_user, upsert_business_connection

logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

WELCOME_TEXT = (
    "💎 <b>Добро пожаловать в Rubuy!</b>\n\n"
    "Маркет подарков Telegram: покупай, продавай и выводи "
    "NFT-гифты за TON.\n\n"
    "Открывай маркет кнопкой меню ниже 👇"
)

_last_update_id = 0


async def _call(method: str, **params) -> dict | list:
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


async def _handle_business_connection(conn: dict) -> None:
    rights = conn.get("rights") or {}
    await upsert_business_connection(
        connection_id=conn["id"],
        business_user_id=(conn.get("user_chat_id")
                          if isinstance(conn.get("user_chat_id"), int)
                          else (conn.get("user") or {}).get("id")),
        can_transfer_gifts=bool(rights.get("can_transfer_and_upgrade_gifts")),
        is_enabled=bool(conn.get("is_enabled", True)),
    )
    logger.info(
        "🏦 Rubuy Bank: business_connection %s (enabled=%s, transfer_gifts=%s)",
        conn["id"], conn.get("is_enabled"),
        rights.get("can_transfer_and_upgrade_gifts"),
    )


async def _handle_start(message: dict) -> None:
    """/start и /start ref_<id> — регистрация + реферальная привязка."""
    sender = message.get("from") or {}
    user_id = sender.get("id")
    if not user_id or sender.get("is_bot"):
        return

    text = (message.get("text") or "").strip()
    referred_by = None
    parts = text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            ref_id = int(parts[1].removeprefix("ref_"))
            if ref_id != user_id:
                referred_by = ref_id
        except ValueError:
            pass

    full_name = " ".join(
        p for p in [sender.get("first_name"), sender.get("last_name")] if p
    )
    await get_or_create_user(
        user_id, sender.get("username", "") or "", full_name,
        referred_by=referred_by,
    )
    if referred_by:
        logger.info("🔗 Реферал: user %s пришёл от %s", user_id, referred_by)

    try:
        await _call("sendMessage", chat_id=user_id, text=WELCOME_TEXT,
                    parse_mode="HTML")
    except Exception as e:
        # юзер мог заблокировать бота — регистрация всё равно состоялась
        logger.warning("Приветствие user %s не доставлено: %s", user_id, e)


async def poll_bot_updates() -> None:
    """Один проход: забираем накопившиеся апдейты и обрабатываем нужные."""
    if not BOT_TOKEN:
        return
    global _last_update_id

    params = {"timeout": 0, "allowed_updates": ["business_connection", "message"]}
    if _last_update_id:
        params["offset"] = _last_update_id + 1
    updates = await _call("getUpdates", **params)
    for update in updates:
        _last_update_id = max(_last_update_id, update["update_id"])

        conn = update.get("business_connection")
        if conn:
            await _handle_business_connection(conn)
            continue

        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        if text.startswith("/start"):
            await _handle_start(message)
