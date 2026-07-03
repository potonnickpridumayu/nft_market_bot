"""Reusable DB query helpers — Postgres (asyncpg) edition.

Публичный интерфейс (имена и возвращаемые формы) полностью совпадает
со старой SQLite-версией, поэтому api_server.py и бот переписывать не нужно.
Разница только внутри: вместо файла nft_market.db — Postgres из DATABASE_URL,
данные больше не слетают при редеплоях Railway.
"""
import os
import logging
import secrets
from typing import Optional

import asyncpg

logger = logging.getLogger(__name__)

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

ALTER TABLE escrow_events ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'processed';
ALTER TABLE escrow_events ADD COLUMN IF NOT EXISTS first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
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


async def update_balance(user_id: int, delta: float):
    pool = await get_pool()
    await pool.execute(
        "UPDATE users SET balance_ton = balance_ton + $1 WHERE user_id=$2",
        delta, user_id,
    )


# ── Gifts ─────────────────────────────────────────────────────────────────────

async def add_gift(owner_id: int, collection_name: str, gift_name: str,
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
               g.rarity, g.image_url, u.username as seller_username
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


async def get_listing(listing_id: int) -> Optional[dict]:
    pool = await get_pool()
    row = await pool.fetchrow(
        """SELECT l.*, g.gift_name, g.collection_name, g.gift_number,
                  g.rarity, g.image_url, g.owner_id,
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
            seller_net = amount_ton - fee_ton - ref_bonus_ton
            await con.execute(
                "UPDATE users SET total_earned=total_earned+$1 WHERE user_id=$2",
                seller_net, seller_id,
            )
    return tx_id


async def get_user_transactions(user_id: int, limit: int = 10) -> list:
    pool = await get_pool()
    rows = await pool.fetch(
        """SELECT t.*, g.gift_name, g.collection_name,
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


async def complete_deposit_intent(intent_id: int, nft_address: str, gift_id: int):
    pool = await get_pool()
    await pool.execute(
        """UPDATE deposit_intents
           SET status='completed', nft_address=$1, gift_id=$2, completed_at=NOW()
           WHERE intent_id=$3""",
        nft_address, gift_id, intent_id,
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