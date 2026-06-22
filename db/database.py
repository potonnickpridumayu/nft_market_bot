"""
Database layer — SQLite via aiosqlite (drop-in replacement: swap for asyncpg + PostgreSQL).
"""
import aiosqlite
import logging
from datetime import datetime

logger = logging.getLogger(__name__)
DB_PATH = "nft_market.db"


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    """Create all tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        -- Users
        CREATE TABLE IF NOT EXISTS users (
            user_id        INTEGER PRIMARY KEY,
            username       TEXT,
            full_name      TEXT,
            referred_by    INTEGER REFERENCES users(user_id),
            balance_ton    REAL    NOT NULL DEFAULT 0,
            total_earned   REAL    NOT NULL DEFAULT 0,
            total_spent    REAL    NOT NULL DEFAULT 0,
            joined_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            is_banned      INTEGER NOT NULL DEFAULT 0
        );

        -- NFT Gifts (Telegram collectibles / TON NFTs)
        CREATE TABLE IF NOT EXISTS gifts (
            gift_id         INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id        INTEGER NOT NULL REFERENCES users(user_id),
            collection_name TEXT    NOT NULL,
            gift_name       TEXT    NOT NULL,
            gift_number     TEXT,                     -- e.g. "#1234"
            rarity          TEXT    DEFAULT 'Common', -- Common/Rare/Epic/Legendary
            image_url       TEXT,
            nft_address     TEXT    UNIQUE,           -- on-chain address (optional)
            acquired_at     TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- Listings (fixed price)
        CREATE TABLE IF NOT EXISTS listings (
            listing_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            gift_id         INTEGER NOT NULL REFERENCES gifts(gift_id),
            seller_id       INTEGER NOT NULL REFERENCES users(user_id),
            price_ton       REAL    NOT NULL,
            description     TEXT,
            status          TEXT    NOT NULL DEFAULT 'active', -- active/sold/cancelled
            views           INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
            sold_at         TEXT
        );

        -- Auctions
        CREATE TABLE IF NOT EXISTS auctions (
            auction_id      INTEGER PRIMARY KEY AUTOINCREMENT,
            gift_id         INTEGER NOT NULL REFERENCES gifts(gift_id),
            seller_id       INTEGER NOT NULL REFERENCES users(user_id),
            start_price     REAL    NOT NULL,
            current_price   REAL    NOT NULL,
            min_step        REAL    NOT NULL DEFAULT 0,
            buyout_price    REAL,                     -- instant buy option
            current_bidder  INTEGER REFERENCES users(user_id),
            status          TEXT    NOT NULL DEFAULT 'active', -- active/ended/cancelled
            ends_at         TEXT    NOT NULL,
            created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- Auction bids log
        CREATE TABLE IF NOT EXISTS bids (
            bid_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            auction_id      INTEGER NOT NULL REFERENCES auctions(auction_id),
            bidder_id       INTEGER NOT NULL REFERENCES users(user_id),
            amount          REAL    NOT NULL,
            placed_at       TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- Transactions
        CREATE TABLE IF NOT EXISTS transactions (
            tx_id           INTEGER PRIMARY KEY AUTOINCREMENT,
            buyer_id        INTEGER NOT NULL REFERENCES users(user_id),
            seller_id       INTEGER NOT NULL REFERENCES users(user_id),
            gift_id         INTEGER NOT NULL REFERENCES gifts(gift_id),
            amount_ton      REAL    NOT NULL,
            fee_ton         REAL    NOT NULL,
            ref_bonus_ton   REAL    NOT NULL DEFAULT 0,
            source          TEXT    NOT NULL DEFAULT 'listing', -- listing/auction
            source_id       INTEGER,
            completed_at    TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- Referral payouts log
        CREATE TABLE IF NOT EXISTS referral_payouts (
            payout_id       INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id     INTEGER NOT NULL REFERENCES users(user_id),
            from_user_id    INTEGER NOT NULL REFERENCES users(user_id),
            tx_id           INTEGER NOT NULL REFERENCES transactions(tx_id),
            amount_ton      REAL    NOT NULL,
            paid_at         TEXT    NOT NULL DEFAULT (datetime('now'))
        );

        -- Indexes for speed
        CREATE INDEX IF NOT EXISTS idx_listings_status   ON listings(status);
        CREATE INDEX IF NOT EXISTS idx_listings_seller   ON listings(seller_id);
        CREATE INDEX IF NOT EXISTS idx_auctions_status   ON auctions(status);
        CREATE INDEX IF NOT EXISTS idx_gifts_owner       ON gifts(owner_id);
        CREATE INDEX IF NOT EXISTS idx_transactions_buyer  ON transactions(buyer_id);
        CREATE INDEX IF NOT EXISTS idx_transactions_seller ON transactions(seller_id);
        """)
        await db.commit()
    logger.info("✅ Database initialised")
