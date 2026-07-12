"""Reusable DB query helpers — Postgres (asyncpg) edition.

Публичный интерфейс (имена и возвращаемые формы) полностью совпадает
со старой SQLite-версией, поэтому api_server.py и бот переписывать не нужно.
Разница только внутри: вместо файла nft_market.db — Postgres из DATABASE_URL,
данные больше не слетают при редеплоях Railway.
"""
import os
import json
import logging
import secrets
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)


def _parse_json_list(v):
    """asyncpg возвращает json/jsonb как сырую строку — декодируем в список."""
    if v is None:
        return []
    return json.loads(v) if isinstance(v, str) else v

# ── Пул соединений ─────────────────────────────────────────────────────────────
# Один общий пул на процесс. Ленивая инициализация: создаётся при первом обращении
# (или заранее через init_db() на старте FastAPI).

_pool: Optional[asyncpg.Pool] = None

async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = os.getenv("DATABASE_URL")
        if not dsn:
            raise RuntimeError("DATABASE_URL не задан — проверь переменные сервиса на Railway")
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=10, command_timeout=30)
    return _pool


# ── Схема ───────────────────────────────────────────────────────────────────────
# Восстановлена по запросам из старого queries.py. Создаётся один раз при старте.
# BIGINT для user_id (это Telegram ID), BIGSERIAL для внутренних id.
# DOUBLE PRECISION для сумм — чтобы код продолжал работать с float (а не Decimal).

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS deposit_intents (
    intent_id    BIGSERIAL PRIMARY KEY,
    user_id      BIGINT REFERENCES users(user_id),
    code         TEXT UNIQUE NOT NULL,          -- уникальный комментарий, напр. GS-12345-A7F3
    status       TEXT NOT NULL DEFAULT 'pending', -- pending/completed/expired
    nft_address  TEXT,                          -- адрес пришедшего NFT (после депозита)
    gift_id      BIGINT REFERENCES gifts(gift_id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
 
CREATE TABLE IF NOT EXISTS escrow_events (
    event_id     BIGSERIAL PRIMARY KEY,
    tx_hash      TEXT UNIQUE NOT NULL,          -- защита от повторной обработки транзакции
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
 
CREATE INDEX IF NOT EXISTS idx_intents_status ON deposit_intents(status);
CREATE INDEX IF NOT EXISTS idx_intents_user   ON deposit_intents(user_id);

CREATE TABLE IF NOT EXISTS users (
    user_id       BIGINT PRIMARY KEY,
    username      TEXT,
    full_name     TEXT,
    referred_by   BIGINT,
    balance_ton   DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_spent   DOUBLE PRECISION NOT NULL DEFAULT 0,
    total_earned  DOUBLE PRECISION NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS gifts (
    gift_id         BIGSERIAL PRIMARY KEY,
    owner_id        BIGINT REFERENCES users(user_id),
    collection_name TEXT,
    gift_name       TEXT,
    gift_number     TEXT DEFAULT '',
    rarity          TEXT DEFAULT 'Common',
    image_url       TEXT DEFAULT '',
    nft_address     TEXT DEFAULT '',
    acquired_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS listings (
    listing_id  BIGSERIAL PRIMARY KEY,
    gift_id     BIGINT REFERENCES gifts(gift_id),
    seller_id   BIGINT REFERENCES users(user_id),
    price_ton   DOUBLE PRECISION NOT NULL,
    description TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',
    views       INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sold_at     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS auctions (
    auction_id     BIGSERIAL PRIMARY KEY,
    gift_id        BIGINT REFERENCES gifts(gift_id),
    seller_id      BIGINT REFERENCES users(user_id),
    start_price    DOUBLE PRECISION NOT NULL,
    current_price  DOUBLE PRECISION NOT NULL,
    min_step       DOUBLE PRECISION NOT NULL DEFAULT 0,
    buyout_price   DOUBLE PRECISION,
    ends_at        TIMESTAMPTZ,
    status         TEXT NOT NULL DEFAULT 'active',
    current_bidder BIGINT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bids (
    bid_id     BIGSERIAL PRIMARY KEY,
    auction_id BIGINT REFERENCES auctions(auction_id),
    bidder_id  BIGINT REFERENCES users(user_id),
    amount     DOUBLE PRECISION NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS transactions (
    tx_id         BIGSERIAL PRIMARY KEY,
    buyer_id      BIGINT REFERENCES users(user_id),
    seller_id     BIGINT REFERENCES users(user_id),
    gift_id       BIGINT REFERENCES gifts(gift_id),
    amount_ton    DOUBLE PRECISION NOT NULL,
    fee_ton       DOUBLE PRECISION NOT NULL DEFAULT 0,
    ref_bonus_ton DOUBLE PRECISION NOT NULL DEFAULT 0,
    source        TEXT,
    source_id     BIGINT,
    completed_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS referral_payouts (
    payout_id    BIGSERIAL PRIMARY KEY,
    referrer_id  BIGINT REFERENCES users(user_id),
    from_user_id BIGINT REFERENCES users(user_id),
    tx_id        BIGINT REFERENCES transactions(tx_id),
    amount_ton   DOUBLE PRECISION NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS withdrawals (
    wd_id        BIGSERIAL PRIMARY KEY,
    user_id      BIGINT REFERENCES users(user_id),
    to_address   TEXT NOT NULL,
    amount_ton   DOUBLE PRECISION NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    tx_hash      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at      TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ
);

ALTER TABLE escrow_events ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'processed';
ALTER TABLE escrow_events ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
ALTER TABLE deposit_intents ADD COLUMN IF NOT EXISTS from_address TEXT;
ALTER TABLE gifts ADD COLUMN IF NOT EXISTS tg_owned_gift_id TEXT;
ALTER TABLE gifts ADD COLUMN IF NOT EXISTS tg_sticker TEXT;   -- file_id анимации (tgs/webm)
ALTER TABLE gifts ADD COLUMN IF NOT EXISTS tg_thumb TEXT;     -- file_id статичной превьюшки
ALTER TABLE gifts ADD COLUMN IF NOT EXISTS tg_backdrop TEXT;  -- JSON: цвета фона + file_id узора
-- Комиссия площадки, реально удержанная с доплаты обмена при принятии оффера
-- (0, если доплаты не было). Хранится по факту, а не пересчитывается по
-- текущему MARKET_FEE — чтобы старые обмены не "переоценивались" задним числом.
ALTER TABLE trade_offers ADD COLUMN IF NOT EXISTS fee_ton DOUBLE PRECISION NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS uq_gifts_tg_owned_gift_id
    ON gifts (tg_owned_gift_id) WHERE tg_owned_gift_id IS NOT NULL;

-- Rubuy Bank: business-подключение бота к аккаунту-сейфу (@twentop).
-- Telegram присылает business_connection update при подключении/отключении/смене прав.
CREATE TABLE IF NOT EXISTS business_connections (
    connection_id      TEXT PRIMARY KEY,
    business_user_id   BIGINT,
    can_transfer_gifts BOOLEAN NOT NULL DEFAULT FALSE,
    is_enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    connected_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Обмен: владелец выставляет свой подарок «на обмен» (без цены), другие
-- пользователи предлагают свой подарок (+ опционально доплату GRAM) взамен.
CREATE TABLE IF NOT EXISTS trade_listings (
    trade_id    BIGSERIAL PRIMARY KEY,
    gift_id     BIGINT REFERENCES gifts(gift_id),
    owner_id    BIGINT REFERENCES users(user_id),
    note        TEXT DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',   -- active/cancelled/completed
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS trade_offers (
    offer_id       BIGSERIAL PRIMARY KEY,
    trade_id       BIGINT REFERENCES trade_listings(trade_id),
    from_user_id   BIGINT REFERENCES users(user_id),
    offered_gift_id BIGINT REFERENCES gifts(gift_id),
    top_up_ton     DOUBLE PRECISION NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'pending', -- pending/accepted/declined/cancelled
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_trade_listings_status ON trade_listings(status);
CREATE INDEX IF NOT EXISTS idx_trade_offers_trade     ON trade_offers(trade_id);
CREATE INDEX IF NOT EXISTS idx_trade_offers_from      ON trade_offers(from_user_id);

-- Мульти-подарочный обмен: и лот, и оффер могут содержать несколько подарков
-- одной стороны. trade_listings.gift_id / trade_offers.offered_gift_id остаются
-- в схеме (старые строки), но новые строки эти колонки не используют —
-- источник истины теперь эти junction-таблицы.
CREATE TABLE IF NOT EXISTS trade_listing_gifts (
    trade_id BIGINT REFERENCES trade_listings(trade_id),
    gift_id  BIGINT REFERENCES gifts(gift_id),
    PRIMARY KEY (trade_id, gift_id)
);
CREATE TABLE IF NOT EXISTS trade_offer_gifts (
    offer_id BIGINT REFERENCES trade_offers(offer_id),
    gift_id  BIGINT REFERENCES gifts(gift_id),
    PRIMARY KEY (offer_id, gift_id)
);
CREATE INDEX IF NOT EXISTS idx_trade_listing_gifts_gift ON trade_listing_gifts(gift_id);
CREATE INDEX IF NOT EXISTS idx_trade_offer_gifts_gift   ON trade_offer_gifts(gift_id);

-- Бэкофилл старых одно-подарочных строк в junction-таблицы. Идемпотентно —
-- безопасно гонять на каждом старте.
INSERT INTO trade_listing_gifts (trade_id, gift_id)
    SELECT trade_id, gift_id FROM trade_listings WHERE gift_id IS NOT NULL
    ON CONFLICT DO NOTHING;
INSERT INTO trade_offer_gifts (offer_id, gift_id)
    SELECT offer_id, offered_gift_id FROM trade_offers WHERE offered_gift_id IS NOT NULL
    ON CONFLICT DO NOTHING;

-- Офферы по цене на обычные лоты Маркета: покупатель предлагает свою цену
-- (не ниже 50% цены лота, см. api_server.py), продавец принимает/отклоняет.
CREATE TABLE IF NOT EXISTS listing_offers (
    offer_id     BIGSERIAL PRIMARY KEY,
    listing_id   BIGINT REFERENCES listings(listing_id),
    from_user_id BIGINT REFERENCES users(user_id),
    amount_ton   DOUBLE PRECISION NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending', -- pending/accepted/declined/cancelled
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_listing_offers_listing ON listing_offers(listing_id);
CREATE INDEX IF NOT EXISTS idx_listing_offers_from    ON listing_offers(from_user_id);
"""


async def init_db():
    """Создаёт таблицы, если их ещё нет. Вызывается на старте FastAPI."""
    pool = await get_pool()
    await pool.execute(SCHEMA_SQL)
    logger.info("DB schema ensured")


async def close_pool():
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ── Users ────────────────────────────────────────────────────────────────────

async def get_or_create_user(user_id: int, username: str, full_name: str,
                              referred_by: Optional[int] = None) -> dict:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    if row:
        return dict(row)
    await pool.execute(
        """INSERT INTO users (user_id, username, full_name, referred_by)
           VALUES ($1,$2,$3,$4) ON CONFLICT (user_id) DO NOTHING""",
        user_id, username, full_name, referred_by,
    )
    row = await pool.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    return dict(row)


async def get_user(user_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
    return dict(row) if row else None


async def try_charge_balance(user_id: int, amount: float) -> bool:
    """Атомарно списать amount, только если хватает баланса. True = списано."""
    pool = await get_pool()
    # Допуск на погрешность float — балансу, равному сумме списания с точностью
    # до отображаемых знаков, не должно ложно не хватать из-за 0.5799999999998.
    row = await pool.fetchrow(
        """UPDATE users SET balance_ton = balance_ton - $1
           WHERE user_id = $2 AND balance_ton >= $1 - 0.000001
           RETURNING user_id""",
        amount, user_id,
    )
    return row is not None


async def update_balance(user_id: int, delta: float):
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET balance_ton = balance_ton + $1 WHERE user_id=$2",
        delta, user_id,
    )

# ── Gifts ─────────────────────────────────────────────────────────────────────

async def add_gift(owner_id: Optional[int], collection_name: str, gift_name: str,
                   gift_number: str = "", rarity: str = "Common",
                   image_url: str = "", nft_address: str = "") -> int:
    pool = await get_pool()
    return await pool.fetchval(
        """INSERT INTO gifts (owner_id, collection_name, gift_name, gift_number,
           rarity, image_url, nft_address)
           VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING gift_id""",
        owner_id, collection_name, gift_name, gift_number, rarity, image_url, nft_address,
    )


async def get_gift(gift_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow("SELECT * FROM gifts WHERE gift_id=$1", gift_id)
    return dict(row) if row else None


async def get_user_gifts(owner_id: int) -> list:
    pool = await get_pool()
    rows = await pool.fetch(
        "SELECT * FROM gifts WHERE owner_id=$1 ORDER BY acquired_at DESC", owner_id
    )
    return [dict(r) for r in rows]


async def transfer_gift(gift_id: int, new_owner_id: int):
    pool = await get_pool()
    await pool.execute(
        "UPDATE gifts SET owner_id=$1, acquired_at=NOW() WHERE gift_id=$2",
        new_owner_id, gift_id,
    )

async def set_gift_tg_id(gift_id: int, tg_owned_gift_id: str):
    pool = await get_pool()
    await pool.execute(
        "UPDATE gifts SET tg_owned_gift_id=$1 WHERE gift_id=$2",
        tg_owned_gift_id, gift_id,
    )


async def delete_gift(gift_id: int):
    """Ручное удаление ошибочной/дубликат-строки (админ-эндпоинт) — напр.
    задвоенный TG-гифт, заведённый до фикса реконсиляции ре-депозитов.
    gift_is_locked (проверяется до вызова) гарантирует отсутствие АКТИВНЫХ
    лотов/аукционов/обменов, но неактивные (cancelled/sold/ended) строки всё
    равно ссылаются на gift_id по FK — чистим их же перед удалением, не трогая
    сами родительские trade_listings/trade_offers (в них может быть несколько
    других гифтов)."""
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute("DELETE FROM listings WHERE gift_id=$1", gift_id)
            await con.execute("DELETE FROM auctions WHERE gift_id=$1", gift_id)
            await con.execute("DELETE FROM trade_listing_gifts WHERE gift_id=$1", gift_id)
            await con.execute("DELETE FROM trade_offer_gifts WHERE gift_id=$1", gift_id)
            # Легаси прямые FK-колонки одно-подарочной эры обмена (см. комментарий
            # у CREATE TABLE trade_listing_gifts) — обнуляем, а не трогаем сам ряд,
            # источник истины для новых обменов теперь junction-таблицы выше.
            await con.execute("UPDATE trade_listings SET gift_id=NULL WHERE gift_id=$1", gift_id)
            await con.execute("UPDATE trade_offers SET offered_gift_id=NULL WHERE offered_gift_id=$1", gift_id)
            await con.execute("DELETE FROM gifts WHERE gift_id=$1", gift_id)


async def get_gift_by_tg_id(tg_owned_gift_id: str):
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT * FROM gifts WHERE tg_owned_gift_id=$1",
        tg_owned_gift_id,
    )


async def get_gift_by_identity(gift_name: str, gift_number: str) -> Optional[dict]:
    """Уникальный TG-гифт однозначно определяется парой (имя коллекции, номер) —
    используется, чтобы распознать повторный занос уже известного физического
    подарка (после вывода из Rubuy Bank и обратного ре-трансфера) вместо
    создания дубликат-строки."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM gifts WHERE gift_name=$1 AND gift_number=$2",
        gift_name, gift_number,
    )
    return dict(row) if row else None


async def reclaim_tg_gift(gift_id: int, new_owner_id: Optional[int], tg_owned_gift_id: str,
                          gift_name: str, gift_number: str,
                          tg_sticker: str, tg_thumb: str, tg_backdrop: str):
    """Тот же физический TG-гифт занесён повторно — Telegram выдаёт новый
    owned_gift_id на каждый ре-трансфер, поэтому lookup только по нему не
    ловит повторный занос: раньше это плодило дубликат-строку с гифтом,
    а старая так и висела с уже неактуальным владельцем навсегда. Здесь вместо
    этого обновляем существующую строку и снимаем любые активные лоты/обмены,
    заведённые под старым владельцем, — иначе они зависли бы на чужом теперь
    подарке (тот же паттерн, что и в accept_trade_offer)."""
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                """UPDATE gifts SET owner_id=$1, tg_owned_gift_id=$2, gift_name=$3,
                       gift_number=$4, tg_sticker=$5, tg_thumb=$6, tg_backdrop=$7,
                       acquired_at=NOW()
                   WHERE gift_id=$8""",
                new_owner_id, tg_owned_gift_id, gift_name, gift_number,
                tg_sticker, tg_thumb, tg_backdrop, gift_id,
            )
            await con.execute(
                "UPDATE listings SET status='cancelled' WHERE gift_id=$1 AND status='active'",
                gift_id,
            )
            await con.execute(
                "UPDATE auctions SET status='ended' WHERE gift_id=$1 AND status='active'",
                gift_id,
            )
            await con.execute(
                """UPDATE trade_listings SET status='cancelled'
                   WHERE status='active' AND trade_id IN (
                       SELECT trade_id FROM trade_listing_gifts WHERE gift_id=$1
                   )""",
                gift_id,
            )
            await con.execute(
                """UPDATE trade_offers SET status='cancelled', resolved_at=NOW()
                   WHERE status='pending' AND offer_id IN (
                       SELECT offer_id FROM trade_offer_gifts WHERE gift_id=$1
                   )""",
                gift_id,
            )


# ── Business connection (Rubuy Bank) ──────────────────────────────────────────

async def upsert_business_connection(connection_id: str, business_user_id: Optional[int],
                                     can_transfer_gifts: bool, is_enabled: bool):
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO business_connections
               (connection_id, business_user_id, can_transfer_gifts, is_enabled, updated_at)
           VALUES ($1,$2,$3,$4,NOW())
           ON CONFLICT (connection_id) DO UPDATE SET
               business_user_id=EXCLUDED.business_user_id,
               can_transfer_gifts=EXCLUDED.can_transfer_gifts,
               is_enabled=EXCLUDED.is_enabled,
               updated_at=NOW()""",
        connection_id, business_user_id, can_transfer_gifts, is_enabled,
    )


async def get_active_business_connection() -> Optional[dict]:
    """Последнее живое подключение. Права на передачу могут появиться позже
    первого подключения — поэтому фильтруем только по is_enabled."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT * FROM business_connections
           WHERE is_enabled=TRUE
           ORDER BY updated_at DESC LIMIT 1"""
    )
    return dict(row) if row else None

async def get_active_listing_for_gift(gift_id: int):
    pool = await get_pool()
    return await pool.fetchrow(
        "SELECT * FROM listings WHERE gift_id=$1 AND status='active'",
        gift_id,
    )

async def update_gift_meta(gift_id: int, gift_name: str, image_url: str):
    pool = await get_pool()
    await pool.execute(
        "UPDATE gifts SET gift_name=$1, image_url=$2 WHERE gift_id=$3",
        gift_name, image_url, gift_id,
    )


async def update_gift_tg_media(gift_id: int, gift_name: str, gift_number: str,
                               tg_sticker: str, tg_thumb: str, tg_backdrop: str = ""):
    """Дообогащение TG-гифта: чистое имя, file_id стикеров, фон с узором."""
    pool = await get_pool()
    await pool.execute(
        """UPDATE gifts SET gift_name=$1, gift_number=$2, tg_sticker=$3, tg_thumb=$4,
               tg_backdrop=$5
           WHERE gift_id=$6""",
        gift_name, gift_number, tg_sticker, tg_thumb, tg_backdrop, gift_id,
    )


# ── Listings ──────────────────────────────────────────────────────────────────

async def create_listing(gift_id: int, seller_id: int, price_ton: float,
                          description: str = "") -> int:
    pool = await get_pool()
    return await pool.fetchval(
        """INSERT INTO listings (gift_id, seller_id, price_ton, description)
           VALUES ($1,$2,$3,$4) RETURNING listing_id""",
        gift_id, seller_id, price_ton, description,
    )


async def get_active_listings(limit: int = 20, offset: int = 0,
                               collection: str = None, max_price: float = None) -> list:
    pool = await get_pool()
    query = """
        SELECT l.*, g.gift_name, g.collection_name, g.gift_number,
               g.rarity, g.image_url, g.nft_address, g.tg_sticker, g.tg_thumb, g.tg_backdrop,
               u.username as seller_username
        FROM listings l
        JOIN gifts g ON l.gift_id = g.gift_id
        JOIN users u ON l.seller_id = u.user_id
        WHERE l.status='active'
    """
    params = []
    if collection:
        params.append(f"%{collection}%")
        query += f" AND g.collection_name ILIKE ${len(params)}"
    if max_price:
        params.append(max_price)
        query += f" AND l.price_ton <= ${len(params)}"
    params.append(limit)
    query += f" ORDER BY l.created_at DESC LIMIT ${len(params)}"
    params.append(offset)
    query += f" OFFSET ${len(params)}"
    rows = await pool.fetch(query, *params)
    return [dict(r) for r in rows]


async def set_listing_price(listing_id: int, price: float) -> bool:
    """Смена цены активного лота владельцем. True = обновлено."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """UPDATE listings SET price_ton=$1
           WHERE listing_id=$2 AND status='active'
           RETURNING listing_id""",
        price, listing_id,
    )
    return row is not None


async def expire_stale_intents(hours: int = 24) -> int:
    """Заявки на NFT-депозит, по которым так и не пришёл трансфер, истекают.
    Денег они не держат — чистка чисто гигиеническая."""
    pool = await get_pool()
    result = await pool.execute(
        """UPDATE deposit_intents SET status='expired'
           WHERE status='pending' AND created_at < NOW() - make_interval(hours => $1)""",
        hours,
    )
    return int(result.split()[-1] or 0)


async def get_listing(listing_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT l.*, g.gift_name, g.collection_name, g.gift_number,
                  g.rarity, g.image_url, g.nft_address, g.owner_id, g.tg_sticker, g.tg_thumb, g.tg_backdrop,
                  u.username as seller_username
           FROM listings l
           JOIN gifts g ON l.gift_id=g.gift_id
           JOIN users u ON l.seller_id=u.user_id
           WHERE l.listing_id=$1""",
        listing_id,
    )
    return dict(row) if row else None


async def cancel_listing(listing_id: int):
    pool = await get_pool()
    await pool.execute(
        "UPDATE listings SET status='cancelled' WHERE listing_id=$1", listing_id
    )


async def mark_listing_sold(listing_id: int):
    pool = await get_pool()
    await pool.execute(
        "UPDATE listings SET status='sold', sold_at=NOW() WHERE listing_id=$1",
        listing_id,
    )


async def increment_views(listing_id: int):
    pool = await get_pool()
    await pool.execute(
        "UPDATE listings SET views=views+1 WHERE listing_id=$1", listing_id
    )


# ── Auctions ──────────────────────────────────────────────────────────────────

async def create_auction(gift_id: int, seller_id: int, start_price: float,
                          min_step: float, buyout_price: Optional[float],
                          ends_at: str) -> int:
    pool = await get_pool()
    return await pool.fetchval(
        """INSERT INTO auctions
           (gift_id, seller_id, start_price, current_price, min_step, buyout_price, ends_at)
           VALUES ($1,$2,$3,$3,$4,$5,$6) RETURNING auction_id""",
        gift_id, seller_id, start_price, min_step, buyout_price, ends_at,
    )


async def get_active_auctions(limit: int = 20, offset: int = 0) -> list:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT a.*, g.gift_name, g.collection_name, g.gift_number,
                  g.rarity, g.image_url, u.username as seller_username
           FROM auctions a
           JOIN gifts g ON a.gift_id=g.gift_id
           JOIN users u ON a.seller_id=u.user_id
           WHERE a.status='active' AND a.ends_at > NOW()
           ORDER BY a.ends_at ASC LIMIT $1 OFFSET $2""",
        limit, offset,
    )
    return [dict(r) for r in rows]


async def get_auction(auction_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT a.*, g.gift_name, g.collection_name, g.gift_number,
                  g.rarity, g.image_url, g.owner_id,
                  u.username as seller_username
           FROM auctions a
           JOIN gifts g ON a.gift_id=g.gift_id
           JOIN users u ON a.seller_id=u.user_id
           WHERE a.auction_id=$1""",
        auction_id,
    )
    return dict(row) if row else None


async def place_bid(auction_id: int, bidder_id: int, amount: float) -> bool:
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            auction = await con.fetchrow(
                "SELECT status, current_price, min_step FROM auctions WHERE auction_id=$1",
                auction_id,
            )
            if not auction or auction["status"] != "active":
                return False
            if amount < auction["current_price"] + auction["min_step"]:
                return False
            await con.execute(
                "UPDATE auctions SET current_price=$1, current_bidder=$2 WHERE auction_id=$3",
                amount, bidder_id, auction_id,
            )
            await con.execute(
                "INSERT INTO bids (auction_id, bidder_id, amount) VALUES ($1,$2,$3)",
                auction_id, bidder_id, amount,
            )
    return True


async def end_auction(auction_id: int):
    pool = await get_pool()
    await pool.execute(
        "UPDATE auctions SET status='ended' WHERE auction_id=$1", auction_id
    )


# ── Обмен (trades) — мульти-подарочный: и лот, и оффер — списки подарков ──────

_GIFT_JSON = """json_build_object(
    'gift_id', g.gift_id, 'gift_name', g.gift_name, 'collection_name', g.collection_name,
    'gift_number', g.gift_number, 'rarity', g.rarity, 'image_url', g.image_url,
    'nft_address', g.nft_address, 'tg_sticker', g.tg_sticker, 'tg_thumb', g.tg_thumb,
    'tg_backdrop', g.tg_backdrop
)"""

_TRADE_LISTING_SELECT = f"""
    SELECT t.trade_id, t.owner_id, t.note, t.status, t.created_at,
           u.username as owner_username,
           (SELECT json_agg({_GIFT_JSON} ORDER BY g.gift_id)
            FROM trade_listing_gifts tlg JOIN gifts g ON g.gift_id=tlg.gift_id
            WHERE tlg.trade_id=t.trade_id) as gifts
    FROM trade_listings t
    JOIN users u ON t.owner_id = u.user_id
"""


async def create_trade_listing(gift_ids: list, owner_id: int, note: str = "") -> int:
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            trade_id = await con.fetchval(
                "INSERT INTO trade_listings (owner_id, note) VALUES ($1,$2) RETURNING trade_id",
                owner_id, note,
            )
            await con.executemany(
                "INSERT INTO trade_listing_gifts (trade_id, gift_id) VALUES ($1,$2)",
                [(trade_id, gid) for gid in gift_ids],
            )
    return trade_id


async def get_active_trade_listings(limit: int = 20, offset: int = 0) -> list:
    pool = await get_pool()
    rows = await pool.fetch(
        _TRADE_LISTING_SELECT + "WHERE t.status='active' ORDER BY t.created_at DESC LIMIT $1 OFFSET $2",
        limit, offset,
    )
    result = []
    for r in rows:
        d = dict(r)
        d["gifts"] = _parse_json_list(d["gifts"])
        result.append(d)
    return result


async def get_trade_listing(trade_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(_TRADE_LISTING_SELECT + "WHERE t.trade_id=$1", trade_id)
    if not row:
        return None
    d = dict(row)
    d["gifts"] = _parse_json_list(d["gifts"])
    return d


async def get_active_trade_listing_for_gift(gift_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT t.* FROM trade_listings t
           JOIN trade_listing_gifts tlg ON tlg.trade_id = t.trade_id
           WHERE tlg.gift_id=$1 AND t.status='active' LIMIT 1""",
        gift_id,
    )
    return dict(row) if row else None


async def cancel_trade_listing(trade_id: int):
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                "UPDATE trade_listings SET status='cancelled' WHERE trade_id=$1", trade_id
            )
            await con.execute(
                """UPDATE trade_offers SET status='cancelled', resolved_at=NOW()
                   WHERE trade_id=$1 AND status='pending'""",
                trade_id,
            )


async def create_trade_offer(trade_id: int, from_user_id: int,
                              gift_ids: list, top_up_ton: float = 0.0) -> int:
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            offer_id = await con.fetchval(
                """INSERT INTO trade_offers (trade_id, from_user_id, top_up_ton)
                   VALUES ($1,$2,$3) RETURNING offer_id""",
                trade_id, from_user_id, top_up_ton,
            )
            await con.executemany(
                "INSERT INTO trade_offer_gifts (offer_id, gift_id) VALUES ($1,$2)",
                [(offer_id, gid) for gid in gift_ids],
            )
    return offer_id


async def get_trade_offer(offer_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT o.*, t.owner_id as to_user_id, t.status as trade_status
           FROM trade_offers o
           JOIN trade_listings t ON o.trade_id = t.trade_id
           WHERE o.offer_id=$1""",
        offer_id,
    )
    return dict(row) if row else None


_TRADE_OFFER_GIFTS_SUBQ = f"""
    (SELECT json_agg({_GIFT_JSON} ORDER BY g.gift_id)
     FROM trade_listing_gifts tlg JOIN gifts g ON g.gift_id=tlg.gift_id
     WHERE tlg.trade_id=t.trade_id) as target_gifts,
    (SELECT json_agg({_GIFT_JSON} ORDER BY g.gift_id)
     FROM trade_offer_gifts tog JOIN gifts g ON g.gift_id=tog.gift_id
     WHERE tog.offer_id=o.offer_id) as offered_gifts
"""


async def get_user_trade_offers(user_id: int) -> dict:
    """Входящие (на мои лоты) и исходящие (мои предложения) офферы пользователя."""
    pool = await get_pool()
    incoming = await pool.fetch(
        f"""SELECT o.*, t.owner_id as to_user_id, u.username as from_username,
                   {_TRADE_OFFER_GIFTS_SUBQ}
           FROM trade_offers o
           JOIN trade_listings t ON o.trade_id = t.trade_id
           JOIN users u ON o.from_user_id = u.user_id
           WHERE t.owner_id=$1 AND o.status='pending'
           ORDER BY o.created_at DESC""",
        user_id,
    )
    outgoing = await pool.fetch(
        f"""SELECT o.*, t.owner_id as to_user_id, u.username as to_username,
                   {_TRADE_OFFER_GIFTS_SUBQ}
           FROM trade_offers o
           JOIN trade_listings t ON o.trade_id = t.trade_id
           JOIN users u ON t.owner_id = u.user_id
           WHERE o.from_user_id=$1 AND o.status='pending'
           ORDER BY o.created_at DESC""",
        user_id,
    )
    def _fmt(rows):
        out = []
        for r in rows:
            d = dict(r)
            d["target_gifts"] = _parse_json_list(d["target_gifts"])
            d["offered_gifts"] = _parse_json_list(d["offered_gifts"])
            out.append(d)
        return out
    return {"incoming": _fmt(incoming), "outgoing": _fmt(outgoing)}


async def decline_trade_offer(offer_id: int):
    pool = await get_pool()
    await pool.execute(
        """UPDATE trade_offers SET status='declined', resolved_at=NOW()
           WHERE offer_id=$1 AND status='pending'""",
        offer_id,
    )


async def cancel_trade_offer(offer_id: int, from_user_id: int) -> bool:
    pool = await get_pool()
    row = await pool.fetchrow(
        """UPDATE trade_offers SET status='cancelled', resolved_at=NOW()
           WHERE offer_id=$1 AND from_user_id=$2 AND status='pending'
           RETURNING offer_id""",
        offer_id, from_user_id,
    )
    return row is not None


async def accept_trade_offer(offer_id: int, market_fee: float = 0.0) -> str:
    """Атомарно меняет владельцев ВСЕХ подарков с обеих сторон + доплату
    (с комиссией площадки market_fee, удерживаемой с доплаты — сами подарки
    в бартере комиссией не облагаются, у них нет цены). Возвращает '' при
    успехе, иначе человеко-читаемую причину отказа (гифт уже не тот и т.п. —
    гонка условий), ничего не откатывая руками, т.к. транзакция сама
    RAISEуется и отменяется."""
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            offer = await con.fetchrow(
                """SELECT o.*, t.owner_id as to_user_id, t.status as trade_status
                   FROM trade_offers o
                   JOIN trade_listings t ON o.trade_id = t.trade_id
                   WHERE o.offer_id=$1 FOR UPDATE""",
                offer_id,
            )
            if not offer or offer["status"] != "pending":
                return "Предложение уже неактуально"
            if offer["trade_status"] != "active":
                return "Лот на обмен уже закрыт"

            target_gift_ids = [r["gift_id"] for r in await con.fetch(
                "SELECT gift_id FROM trade_listing_gifts WHERE trade_id=$1", offer["trade_id"])]
            offered_gift_ids = [r["gift_id"] for r in await con.fetch(
                "SELECT gift_id FROM trade_offer_gifts WHERE offer_id=$1", offer_id)]
            if not target_gift_ids or not offered_gift_ids:
                return "Лот пуст — обмен невозможен"

            target_rows = await con.fetch(
                "SELECT gift_id, owner_id FROM gifts WHERE gift_id = ANY($1::bigint[]) FOR UPDATE",
                target_gift_ids,
            )
            offered_rows = await con.fetch(
                "SELECT gift_id, owner_id FROM gifts WHERE gift_id = ANY($1::bigint[]) FOR UPDATE",
                offered_gift_ids,
            )
            if len(target_rows) != len(target_gift_ids) or any(
                    r["owner_id"] != offer["to_user_id"] for r in target_rows):
                return "Ваш подарок больше не у вас"
            if len(offered_rows) != len(offered_gift_ids) or any(
                    r["owner_id"] != offer["from_user_id"] for r in offered_rows):
                return "Предложенный подарок больше не у отправителя"

            fee_ton = 0.0
            if offer["top_up_ton"] > 0:
                charged = await con.fetchrow(
                    """UPDATE users SET balance_ton = balance_ton - $1
                       WHERE user_id=$2 AND balance_ton >= $1 - 0.000001
                       RETURNING user_id""",
                    offer["top_up_ton"], offer["from_user_id"],
                )
                if not charged:
                    return "У отправителя не хватает баланса на доплату"
                fee_ton = offer["top_up_ton"] * market_fee
                await con.execute(
                    "UPDATE users SET balance_ton = balance_ton + $1 WHERE user_id=$2",
                    offer["top_up_ton"] - fee_ton, offer["to_user_id"],
                )
                logger.info(
                    "🔄💰 Обмен offer_id=%s: доплата %.4f GRAM, комиссия %.4f GRAM удержана (получатель %s получил %.4f)",
                    offer_id, offer["top_up_ton"], fee_ton, offer["to_user_id"],
                    offer["top_up_ton"] - fee_ton,
                )

            await con.execute(
                "UPDATE gifts SET owner_id=$1, acquired_at=NOW() WHERE gift_id = ANY($2::bigint[])",
                offer["to_user_id"], offered_gift_ids,
            )
            await con.execute(
                "UPDATE gifts SET owner_id=$1, acquired_at=NOW() WHERE gift_id = ANY($2::bigint[])",
                offer["from_user_id"], target_gift_ids,
            )
            # Пока оффер висел pending, отправитель мог параллельно выставить
            # предложенный подарок на продажу/аукцион на СВОЁМ прежнем владении —
            # такой лот не блокировался (лочится только сам предмет обмена, а не
            # то, что человек ЕЩЁ МОЖЕТ предложить). После смены владельца любой
            # такой лот стал бы висеть под старым продавцом на чужом подарке —
            # закрываем все такие на всякий случай.
            swapped_gift_ids = target_gift_ids + offered_gift_ids
            await con.execute(
                "UPDATE listings SET status='cancelled' WHERE gift_id = ANY($1::bigint[]) AND status='active'",
                swapped_gift_ids,
            )
            await con.execute(
                "UPDATE auctions SET status='ended' WHERE gift_id = ANY($1::bigint[]) AND status='active'",
                swapped_gift_ids,
            )
            await con.execute(
                "UPDATE trade_listings SET status='completed' WHERE trade_id=$1", offer["trade_id"]
            )
            await con.execute(
                """UPDATE trade_offers SET status='accepted', resolved_at=NOW(), fee_ton=$1
                   WHERE offer_id=$2""",
                fee_ton, offer_id,
            )
            await con.execute(
                """UPDATE trade_offers SET status='declined', resolved_at=NOW()
                   WHERE trade_id=$1 AND offer_id!=$2 AND status='pending'""",
                offer["trade_id"], offer_id,
            )
    return ""


# ── Transactions ──────────────────────────────────────────────────────────────

async def record_transaction(buyer_id: int, seller_id: int, gift_id: int,
                              amount_ton: float, fee_ton: float, ref_bonus_ton: float,
                              source: str, source_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            tx_id = await con.fetchval(
                """INSERT INTO transactions
                   (buyer_id, seller_id, gift_id, amount_ton, fee_ton, ref_bonus_ton, source, source_id)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING tx_id""",
                buyer_id, seller_id, gift_id, amount_ton, fee_ton, ref_bonus_ton, source, source_id,
            )
            await con.execute(
                "UPDATE users SET total_spent=total_spent+$1 WHERE user_id=$2",
                amount_ton, buyer_id,
            )
            # Реф-бонус идёт из комиссии площадки, поэтому в заработанное
            # продавцу засчитываем ровно amount − fee (как и реально выплачено).
            seller_net = amount_ton - fee_ton
            await con.execute(
                "UPDATE users SET total_earned=total_earned+$1 WHERE user_id=$2",
                seller_net, seller_id,
            )
    return tx_id


async def get_user_transactions(user_id: int, limit: int = 10) -> list:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT t.*, g.gift_name, g.collection_name, g.gift_number, g.nft_address,
                  b.username as buyer_username, s.username as seller_username
           FROM transactions t
           JOIN gifts g ON t.gift_id=g.gift_id
           JOIN users b ON t.buyer_id=b.user_id
           JOIN users s ON t.seller_id=s.user_id
           WHERE t.buyer_id=$1 OR t.seller_id=$1
           ORDER BY t.completed_at DESC LIMIT $2""",
        user_id, limit,
    )
    return [dict(r) for r in rows]


async def get_user_completed_trades(user_id: int, limit: int = 10) -> list:
    """Завершённые обмены (принятые офферы), где пользователь — любая из сторон."""
    pool = await get_pool()
    rows = await pool.fetch(
        f"""SELECT o.offer_id, o.resolved_at as completed_at, o.top_up_ton, o.fee_ton,
                   o.from_user_id, t.owner_id as to_user_id,
                   ufrom.username as from_username, uto.username as to_username,
                   {_TRADE_OFFER_GIFTS_SUBQ}
           FROM trade_offers o
           JOIN trade_listings t ON o.trade_id = t.trade_id
           JOIN users ufrom ON o.from_user_id = ufrom.user_id
           JOIN users uto ON t.owner_id = uto.user_id
           WHERE o.status='accepted' AND (o.from_user_id=$1 OR t.owner_id=$1)
           ORDER BY o.resolved_at DESC LIMIT $2""",
        user_id, limit,
    )
    result = []
    for r in rows:
        d = dict(r)
        d["target_gifts"] = _parse_json_list(d["target_gifts"])
        d["offered_gifts"] = _parse_json_list(d["offered_gifts"])
        result.append(d)
    return result


async def get_user_deal_count(user_id: int) -> int:
    """Полное число завершённых сделок (продажи/покупки + принятые обмены),
    независимо от того, сколько из них реально подгружается в историю."""
    pool = await get_pool()
    tx_count = await pool.fetchval(
        "SELECT COUNT(*) FROM transactions WHERE buyer_id=$1 OR seller_id=$1", user_id
    )
    trade_count = await pool.fetchval(
        """SELECT COUNT(*) FROM trade_offers o
           JOIN trade_listings t ON o.trade_id = t.trade_id
           WHERE o.status='accepted' AND (o.from_user_id=$1 OR t.owner_id=$1)""",
        user_id,
    )
    return tx_count + trade_count


# ── Офферы по цене на лоты Маркета ───────────────────────────────────────────

async def create_listing_offer(listing_id: int, from_user_id: int, amount_ton: float) -> int:
    pool = await get_pool()
    return await pool.fetchval(
        """INSERT INTO listing_offers (listing_id, from_user_id, amount_ton)
           VALUES ($1,$2,$3) RETURNING offer_id""",
        listing_id, from_user_id, amount_ton,
    )


async def get_listing_offer(offer_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT o.*, l.seller_id, l.gift_id, l.price_ton, l.status as listing_status
           FROM listing_offers o JOIN listings l ON o.listing_id = l.listing_id
           WHERE o.offer_id=$1""",
        offer_id,
    )
    return dict(row) if row else None


async def get_user_listing_offers(user_id: int) -> dict:
    """Входящие (на мои лоты) и исходящие (мои предложения цены) офферы."""
    pool = await get_pool()
    incoming = await pool.fetch(
        """SELECT o.*, l.price_ton, g.gift_name, g.gift_number, g.nft_address,
                  u.username as from_username
           FROM listing_offers o
           JOIN listings l ON o.listing_id = l.listing_id
           JOIN gifts g ON l.gift_id = g.gift_id
           JOIN users u ON o.from_user_id = u.user_id
           WHERE l.seller_id=$1 AND o.status='pending'
           ORDER BY o.created_at DESC""",
        user_id,
    )
    outgoing = await pool.fetch(
        """SELECT o.*, l.price_ton, g.gift_name, g.gift_number, g.nft_address,
                  u.username as to_username
           FROM listing_offers o
           JOIN listings l ON o.listing_id = l.listing_id
           JOIN gifts g ON l.gift_id = g.gift_id
           JOIN users u ON l.seller_id = u.user_id
           WHERE o.from_user_id=$1 AND o.status='pending'
           ORDER BY o.created_at DESC""",
        user_id,
    )
    return {"incoming": [dict(r) for r in incoming], "outgoing": [dict(r) for r in outgoing]}


async def decline_listing_offer(offer_id: int):
    pool = await get_pool()
    await pool.execute(
        """UPDATE listing_offers SET status='declined', resolved_at=NOW()
           WHERE offer_id=$1 AND status='pending'""",
        offer_id,
    )


async def cancel_listing_offer(offer_id: int, from_user_id: int) -> bool:
    pool = await get_pool()
    row = await pool.fetchrow(
        """UPDATE listing_offers SET status='cancelled', resolved_at=NOW()
           WHERE offer_id=$1 AND from_user_id=$2 AND status='pending'
           RETURNING offer_id""",
        offer_id, from_user_id,
    )
    return row is not None


async def accept_listing_offer(offer_id: int, market_fee: float, referral_bonus_percent: float):
    """Атомарно продаёт лот покупателю по цене оффера (а не цене лота).
    Возвращает (error, result): error='' и result-дикт при успехе."""
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            offer = await con.fetchrow(
                """SELECT o.*, l.seller_id, l.gift_id, l.status as listing_status
                   FROM listing_offers o JOIN listings l ON o.listing_id = l.listing_id
                   WHERE o.offer_id=$1 FOR UPDATE""",
                offer_id,
            )
            if not offer or offer["status"] != "pending":
                return "Предложение уже неактуально", None
            if offer["listing_status"] != "active":
                return "Лот уже неактивен", None

            price = offer["amount_ton"]
            buyer_id = offer["from_user_id"]
            seller_id = offer["seller_id"]

            seller = await con.fetchrow(
                "SELECT referred_by FROM users WHERE user_id=$1 FOR UPDATE", seller_id
            )
            fee = price * market_fee
            ref_bonus = 0.0
            if seller and seller["referred_by"] and seller["referred_by"] != buyer_id:
                ref_bonus = price * referral_bonus_percent

            charged = await con.fetchrow(
                """UPDATE users SET balance_ton = balance_ton - $1
                   WHERE user_id=$2 AND balance_ton >= $1 - 0.000001
                   RETURNING user_id""",
                price, buyer_id,
            )
            if not charged:
                return "У покупателя не хватает баланса", None

            # Реф-бонус платится ИЗ комиссии площадки (как в buy_listing):
            # продавец получает ровно price − fee, наша доля = fee − ref_bonus.
            seller_net = price - fee
            await con.execute(
                "UPDATE users SET balance_ton = balance_ton + $1 WHERE user_id=$2",
                seller_net, seller_id,
            )
            await con.execute(
                "UPDATE gifts SET owner_id=$1, acquired_at=NOW() WHERE gift_id=$2",
                buyer_id, offer["gift_id"],
            )
            await con.execute(
                "UPDATE listings SET status='sold', sold_at=NOW() WHERE listing_id=$1",
                offer["listing_id"],
            )
            tx_id = await con.fetchval(
                """INSERT INTO transactions
                   (buyer_id, seller_id, gift_id, amount_ton, fee_ton, ref_bonus_ton, source, source_id)
                   VALUES ($1,$2,$3,$4,$5,$6,'listing_offer',$7) RETURNING tx_id""",
                buyer_id, seller_id, offer["gift_id"], price, fee, ref_bonus, offer["listing_id"],
            )
            await con.execute(
                "UPDATE users SET total_spent=total_spent+$1 WHERE user_id=$2", price, buyer_id
            )
            await con.execute(
                "UPDATE users SET total_earned=total_earned+$1 WHERE user_id=$2", seller_net, seller_id
            )
            if ref_bonus > 0 and seller["referred_by"]:
                await con.execute(
                    "UPDATE users SET balance_ton = balance_ton + $1 WHERE user_id=$2",
                    ref_bonus, seller["referred_by"],
                )
                await con.execute(
                    """INSERT INTO referral_payouts (referrer_id, from_user_id, tx_id, amount_ton)
                       VALUES ($1,$2,$3,$4)""",
                    seller["referred_by"], seller_id, tx_id, ref_bonus,
                )
            await con.execute(
                "UPDATE listing_offers SET status='accepted', resolved_at=NOW() WHERE offer_id=$1",
                offer_id,
            )
            await con.execute(
                """UPDATE listing_offers SET status='declined', resolved_at=NOW()
                   WHERE listing_id=$1 AND offer_id!=$2 AND status='pending'""",
                offer["listing_id"], offer_id,
            )
    return "", {
        "buyer_id": buyer_id, "seller_id": seller_id, "gift_id": offer["gift_id"],
        "price": price, "fee": fee, "seller_net": seller_net, "listing_id": offer["listing_id"],
    }


# ── Referrals ─────────────────────────────────────────────────────────────────

async def get_referral_count(user_id: int) -> int:
    pool = await get_pool()
    val = await pool.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1", user_id)
    return val or 0


async def get_referral_earnings(user_id: int) -> float:
    pool = await get_pool()
    val = await pool.fetchval(
        "SELECT COALESCE(SUM(amount_ton),0) FROM referral_payouts WHERE referrer_id=$1",
        user_id,
    )
    return float(val or 0.0)


async def record_referral_payout(referrer_id: int, from_user_id: int,
                                  tx_id: int, amount_ton: float):
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO referral_payouts (referrer_id, from_user_id, tx_id, amount_ton)
           VALUES ($1,$2,$3,$4)""",
        referrer_id, from_user_id, tx_id, amount_ton,
    )

async def get_referral_stats(user_id: int):
    pool = await get_pool()
    invited = await pool.fetchval(
        "SELECT COUNT(*) FROM users WHERE referred_by = $1", user_id
    ) or 0
    earned = await pool.fetchval(
        "SELECT COALESCE(SUM(amount_ton), 0) FROM referral_payouts WHERE referrer_id = $1",
        user_id,
    ) or 0
    return {"invited": int(invited), "earned_ton": float(earned)}


# ── Admin stats ───────────────────────────────────────────────────────────────

async def get_platform_stats() -> dict:
    pool = await get_pool()
    users = await pool.fetchval("SELECT COUNT(*) FROM users")
    listings = await pool.fetchval("SELECT COUNT(*) FROM listings WHERE status='active'")
    auctions = await pool.fetchval("SELECT COUNT(*) FROM auctions WHERE status='active'")
    volume = await pool.fetchval("SELECT COALESCE(SUM(amount_ton),0) FROM transactions")
    fees = await pool.fetchval("SELECT COALESCE(SUM(fee_ton),0) FROM transactions")
    return {
        "users": users or 0,
        "active_listings": listings or 0,
        "active_auctions": auctions or 0,
        "total_volume": float(volume or 0),
        "total_fees": float(fees or 0),
    }


async def get_or_create_deposit_intent(user_id: int) -> dict:
    """Выдаёт продавцу код депозита. Если pending-intent уже есть — возвращает его же,
    чтобы у одного юзера не плодились коды."""
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM deposit_intents WHERE user_id=$1 AND status='pending'",
        user_id,
    )
    if row:
        return dict(row)
    code = f"GS-{user_id}-{secrets.token_hex(2).upper()}"
    row = await pool.fetchrow(
        """INSERT INTO deposit_intents (user_id, code)
           VALUES ($1,$2) RETURNING *""",
        user_id, code,
    )
    return dict(row)


async def get_pending_intent_by_code(code: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM deposit_intents WHERE code=$1 AND status='pending'",
        code,
    )
    return dict(row) if row else None


async def complete_deposit_intent(intent_id: int, nft_address: str, gift_id: int,
                                  from_address: str = ""):
    pool = await get_pool()
    await pool.execute(
        """UPDATE deposit_intents
           SET status='completed', nft_address=$1, gift_id=$2,
               from_address=$4, completed_at=NOW()
           WHERE intent_id=$3""",
        nft_address, gift_id, intent_id, from_address,
    )


async def is_event_processed(tx_hash: str) -> bool:
    """Обработано терминально. 'unmatched_pending' — НЕ терминально,
    такой трансфер поллер будет пытаться сматчить снова."""
    pool = await get_pool()
    status = await pool.fetchval(
        "SELECT status FROM escrow_events WHERE tx_hash=$1", tx_hash
    )
    return status is not None and status != "unmatched_pending"


async def mark_event_processed(tx_hash: str, status: str = "processed"):
    pool = await get_pool()
    await pool.execute(
        """INSERT INTO escrow_events (tx_hash, status) VALUES ($1, $2)
           ON CONFLICT (tx_hash) DO UPDATE SET status = EXCLUDED.status""",
        tx_hash, status,
    )


async def touch_unmatched_event(tx_hash: str):
    """Фиксирует несматченный трансфер как 'unmatched_pending' (если ещё не записан)
    и возвращает first_seen_at — от него считаем grace-период."""
    pool = await get_pool()
    return await pool.fetchval(
        """INSERT INTO escrow_events (tx_hash, status)
           VALUES ($1, 'unmatched_pending')
           ON CONFLICT (tx_hash) DO UPDATE SET tx_hash = EXCLUDED.tx_hash
           RETURNING first_seen_at""",
        tx_hash,
    )

async def get_latest_intent_for_user(user_id: int) -> Optional[dict]:
    """Последний intent юзера — для опроса статуса депозита с фронта."""
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT * FROM deposit_intents WHERE user_id=$1
           ORDER BY created_at DESC LIMIT 1""",
        user_id,
    )
    return dict(row) if row else None

async def get_deposit_source(user_id: int, nft_address: str) -> Optional[str]:
    """Адрес, с которого юзер депонировал этот NFT."""
    pool = await get_pool()
    return await pool.fetchval(
        """SELECT from_address FROM deposit_intents
           WHERE user_id=$1 AND nft_address=$2 AND status='completed'
                 AND from_address <> ''
           ORDER BY completed_at DESC LIMIT 1""",
        user_id, nft_address,
    )


async def set_listing_status(listing_id: int, status: str):
    pool = await get_pool()
    await pool.execute(
        "UPDATE listings SET status=$2 WHERE listing_id=$1", listing_id, status
    )


async def release_gift(gift_id: int):
    """NFT ушёл с платформы — убираем подарок из инвентаря (owner→NULL)."""
    pool = await get_pool()
    await pool.execute(
        "UPDATE gifts SET owner_id=NULL WHERE gift_id=$1", gift_id
    )

async def set_gift_owner(gift_id: int, owner_id: Optional[int]):
    pool = await get_pool()
    await pool.execute(
        "UPDATE gifts SET owner_id=$1 WHERE gift_id=$2", owner_id, gift_id
    )


async def gift_is_locked(gift_id: int) -> bool:
    """Гифт занят активным лотом, аукционом, обменом (в т.ч. как один из
    нескольких подарков лота/оффера) — выводить/перевыставлять нельзя."""
    pool = await get_pool()
    return await pool.fetchval(
        """SELECT EXISTS(SELECT 1 FROM listings
                         WHERE gift_id=$1 AND status='active')
               OR EXISTS(SELECT 1 FROM auctions
                         WHERE gift_id=$1 AND status='active')
               OR EXISTS(SELECT 1 FROM trade_listing_gifts tlg
                         JOIN trade_listings t ON tlg.trade_id=t.trade_id
                         WHERE tlg.gift_id=$1 AND t.status='active')
               OR EXISTS(SELECT 1 FROM trade_offer_gifts tog
                         JOIN trade_offers o ON tog.offer_id=o.offer_id
                         WHERE tog.gift_id=$1 AND o.status='pending')""",
        gift_id,
    )

async def get_gift_locks(gift_id: int) -> dict:
    """То же, что gift_is_locked, но возвращает КОНКРЕТНЫЕ id — для админ-
    диагностики зависших гифтов (прямой доступ к Postgres с локальной машины
    порезан провайдером, см. rubuy_project_overview)."""
    pool = await get_pool()
    listings = [dict(r) for r in await pool.fetch(
        "SELECT listing_id, status FROM listings WHERE gift_id=$1 AND status='active'", gift_id)]
    auctions = [dict(r) for r in await pool.fetch(
        "SELECT auction_id, status FROM auctions WHERE gift_id=$1 AND status='active'", gift_id)]
    trade_listings_rows = [dict(r) for r in await pool.fetch(
        """SELECT t.trade_id, t.status FROM trade_listing_gifts tlg
           JOIN trade_listings t ON tlg.trade_id=t.trade_id
           WHERE tlg.gift_id=$1 AND t.status='active'""", gift_id)]
    trade_offers_rows = [dict(r) for r in await pool.fetch(
        """SELECT o.offer_id, o.trade_id, o.status FROM trade_offer_gifts tog
           JOIN trade_offers o ON tog.offer_id=o.offer_id
           WHERE tog.gift_id=$1 AND o.status='pending'""", gift_id)]
    return {
        "listings": listings, "auctions": auctions,
        "trade_listings": trade_listings_rows, "trade_offers": trade_offers_rows,
    }


async def get_gift_by_nft_address(nft_address: str) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        "SELECT * FROM gifts WHERE nft_address=$1 ORDER BY gift_id DESC LIMIT 1",
        nft_address,
    )
    return dict(row) if row else None

# ── Withdrawals (C-4) ─────────────────────────────────────────────────────────

async def create_withdrawal(user_id: int, to_address: str, amount_ton: float) -> int:
    """Атомарно: списать баланс + создать запись pending. 0 — если не хватило средств."""
    pool = await get_pool()
    async with pool.acquire() as con:
        async with con.transaction():
            bal = await con.fetchval(
                "SELECT balance_ton FROM users WHERE user_id=$1 FOR UPDATE", user_id)
            if bal is None or float(bal) < amount_ton - 1e-6:
                return 0
            await con.execute(
                "UPDATE users SET balance_ton = balance_ton - $1 WHERE user_id=$2",
                amount_ton, user_id)
            return await con.fetchval(
                """INSERT INTO withdrawals (user_id, to_address, amount_ton)
                   VALUES ($1,$2,$3) RETURNING wd_id""",
                user_id, to_address, amount_ton)


async def mark_withdrawal_sent(wd_id: int, tx_hash: str):
    pool = await get_pool()
    await pool.execute(
        "UPDATE withdrawals SET status='sent', tx_hash=$1, sent_at=NOW() WHERE wd_id=$2 AND status='pending'",
        tx_hash, wd_id)


async def confirm_withdrawal(wd_id: int) -> bool:
    """Поллер нашёл исходящую транзакцию в блокчейне. True — если реально перевели в confirmed."""
    pool = await get_pool()
    res = await pool.execute(
        """UPDATE withdrawals SET status='confirmed', confirmed_at=NOW()
           WHERE wd_id=$1 AND status IN ('pending','sent')""", wd_id)
    return res.endswith("1")


async def refund_stale_withdrawals(grace_minutes: int = 15) -> list:
    """Вернуть баланс по выводам без ончейн-подтверждения дольше грейс-периода.

    Статус-гард в UPDATE защищает от двойного возврата даже при гонке."""
    pool = await get_pool()
    refunded = []
    async with pool.acquire() as con:
        rows = await con.fetch(
            """SELECT wd_id, user_id, amount_ton FROM withdrawals
               WHERE status IN ('pending','sent')
                 AND created_at < NOW() - ($1 || ' minutes')::interval""",
            str(int(grace_minutes)))
        for r in rows:
            async with con.transaction():
                res = await con.execute(
                    "UPDATE withdrawals SET status='refunded' WHERE wd_id=$1 AND status IN ('pending','sent')",
                    r["wd_id"])
                if res.endswith("1"):
                    await con.execute(
                        "UPDATE users SET balance_ton = balance_ton + $1 WHERE user_id=$2",
                        r["amount_ton"], r["user_id"])
                    refunded.append(dict(r))
    return refunded