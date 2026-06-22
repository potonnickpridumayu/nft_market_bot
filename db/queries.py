"""Reusable DB query helpers — keep handlers thin."""
from typing import Optional
import aiosqlite
import logging

logger = logging.getLogger(__name__)
DB_PATH = "nft_market.db"


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


# ── Users ────────────────────────────────────────────────────────────────────

async def get_or_create_user(user_id: int, username: str, full_name: str,
                              referred_by: Optional[int] = None) -> dict:
    db = await get_db()
    try:
        row = await db.execute_fetchall(
            "SELECT * FROM users WHERE user_id=?", (user_id,)
        )
        if row:
            return dict(row[0])
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, full_name, referred_by) VALUES (?,?,?,?)",
            (user_id, username, full_name, referred_by)
        )
        await db.commit()
        row = await db.execute_fetchall("SELECT * FROM users WHERE user_id=?", (user_id,))
        return dict(row[0])
    finally:
        await db.close()


async def get_user(user_id: int) -> Optional[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM users WHERE user_id=?", (user_id,))
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def update_balance(user_id: int, delta: float):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE users SET balance_ton = balance_ton + ? WHERE user_id=?",
            (delta, user_id)
        )
        await db.commit()
    finally:
        await db.close()


# ── Gifts ─────────────────────────────────────────────────────────────────────

async def add_gift(owner_id: int, collection_name: str, gift_name: str,
                   gift_number: str = "", rarity: str = "Common",
                   image_url: str = "", nft_address: str = "") -> int:
    db = await get_db()
    try:
        cur = await db.execute(
            """INSERT INTO gifts (owner_id, collection_name, gift_name, gift_number,
               rarity, image_url, nft_address) VALUES (?,?,?,?,?,?,?)""",
            (owner_id, collection_name, gift_name, gift_number, rarity, image_url, nft_address)
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def get_gift(gift_id: int) -> Optional[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall("SELECT * FROM gifts WHERE gift_id=?", (gift_id,))
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def get_user_gifts(owner_id: int) -> list:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT * FROM gifts WHERE owner_id=? ORDER BY acquired_at DESC", (owner_id,)
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def transfer_gift(gift_id: int, new_owner_id: int):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE gifts SET owner_id=?, acquired_at=datetime('now') WHERE gift_id=?",
            (new_owner_id, gift_id)
        )
        await db.commit()
    finally:
        await db.close()


# ── Listings ──────────────────────────────────────────────────────────────────

async def create_listing(gift_id: int, seller_id: int, price_ton: float,
                          description: str = "") -> int:
    db = await get_db()
    try:
        cur = await db.execute(
            "INSERT INTO listings (gift_id, seller_id, price_ton, description) VALUES (?,?,?,?)",
            (gift_id, seller_id, price_ton, description)
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def get_active_listings(limit: int = 20, offset: int = 0,
                               collection: str = None, max_price: float = None) -> list:
    db = await get_db()
    try:
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
            query += " AND g.collection_name LIKE ?"
            params.append(f"%{collection}%")
        if max_price:
            query += " AND l.price_ton <= ?"
            params.append(max_price)
        query += " ORDER BY l.created_at DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        rows = await db.execute_fetchall(query, params)
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_listing(listing_id: int) -> Optional[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT l.*, g.gift_name, g.collection_name, g.gift_number,
                      g.rarity, g.image_url, g.owner_id,
                      u.username as seller_username
               FROM listings l
               JOIN gifts g ON l.gift_id=g.gift_id
               JOIN users u ON l.seller_id=u.user_id
               WHERE l.listing_id=?""",
            (listing_id,)
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def cancel_listing(listing_id: int):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE listings SET status='cancelled' WHERE listing_id=?", (listing_id,)
        )
        await db.commit()
    finally:
        await db.close()


async def increment_views(listing_id: int):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE listings SET views=views+1 WHERE listing_id=?", (listing_id,)
        )
        await db.commit()
    finally:
        await db.close()


# ── Auctions ──────────────────────────────────────────────────────────────────

async def create_auction(gift_id: int, seller_id: int, start_price: float,
                          min_step: float, buyout_price: Optional[float],
                          ends_at: str) -> int:
    db = await get_db()
    try:
        cur = await db.execute(
            """INSERT INTO auctions
               (gift_id, seller_id, start_price, current_price, min_step, buyout_price, ends_at)
               VALUES (?,?,?,?,?,?,?)""",
            (gift_id, seller_id, start_price, start_price, min_step, buyout_price, ends_at)
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def get_active_auctions(limit: int = 20, offset: int = 0) -> list:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT a.*, g.gift_name, g.collection_name, g.gift_number,
                      g.rarity, g.image_url, u.username as seller_username
               FROM auctions a
               JOIN gifts g ON a.gift_id=g.gift_id
               JOIN users u ON a.seller_id=u.user_id
               WHERE a.status='active' AND a.ends_at > datetime('now')
               ORDER BY a.ends_at ASC LIMIT ? OFFSET ?""",
            (limit, offset)
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_auction(auction_id: int) -> Optional[dict]:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT a.*, g.gift_name, g.collection_name, g.gift_number,
                      g.rarity, g.image_url, g.owner_id,
                      u.username as seller_username
               FROM auctions a
               JOIN gifts g ON a.gift_id=g.gift_id
               JOIN users u ON a.seller_id=u.user_id
               WHERE a.auction_id=?""",
            (auction_id,)
        )
        return dict(rows[0]) if rows else None
    finally:
        await db.close()


async def place_bid(auction_id: int, bidder_id: int, amount: float) -> bool:
    db = await get_db()
    try:
        auction = await get_auction(auction_id)
        if not auction or auction["status"] != "active":
            return False
        if amount < auction["current_price"] + auction["min_step"]:
            return False
        await db.execute(
            "UPDATE auctions SET current_price=?, current_bidder=? WHERE auction_id=?",
            (amount, bidder_id, auction_id)
        )
        await db.execute(
            "INSERT INTO bids (auction_id, bidder_id, amount) VALUES (?,?,?)",
            (auction_id, bidder_id, amount)
        )
        await db.commit()
        return True
    finally:
        await db.close()


async def end_auction(auction_id: int):
    db = await get_db()
    try:
        await db.execute(
            "UPDATE auctions SET status='ended' WHERE auction_id=?", (auction_id,)
        )
        await db.commit()
    finally:
        await db.close()


# ── Transactions ──────────────────────────────────────────────────────────────

async def record_transaction(buyer_id: int, seller_id: int, gift_id: int,
                              amount_ton: float, fee_ton: float, ref_bonus_ton: float,
                              source: str, source_id: int) -> int:
    db = await get_db()
    try:
        cur = await db.execute(
            """INSERT INTO transactions
               (buyer_id, seller_id, gift_id, amount_ton, fee_ton, ref_bonus_ton, source, source_id)
               VALUES (?,?,?,?,?,?,?,?)""",
            (buyer_id, seller_id, gift_id, amount_ton, fee_ton, ref_bonus_ton, source, source_id)
        )
        await db.execute(
            "UPDATE users SET total_spent=total_spent+? WHERE user_id=?", (amount_ton, buyer_id)
        )
        seller_net = amount_ton - fee_ton - ref_bonus_ton
        await db.execute(
            "UPDATE users SET total_earned=total_earned+? WHERE user_id=?", (seller_net, seller_id)
        )
        await db.commit()
        return cur.lastrowid
    finally:
        await db.close()


async def get_user_transactions(user_id: int, limit: int = 10) -> list:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            """SELECT t.*, g.gift_name, g.collection_name,
                      b.username as buyer_username, s.username as seller_username
               FROM transactions t
               JOIN gifts g ON t.gift_id=g.gift_id
               JOIN users b ON t.buyer_id=b.user_id
               JOIN users s ON t.seller_id=s.user_id
               WHERE t.buyer_id=? OR t.seller_id=?
               ORDER BY t.completed_at DESC LIMIT ?""",
            (user_id, user_id, limit)
        )
        return [dict(r) for r in rows]
    finally:
        await db.close()


# ── Referrals ─────────────────────────────────────────────────────────────────

async def get_referral_count(user_id: int) -> int:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT COUNT(*) as cnt FROM users WHERE referred_by=?", (user_id,)
        )
        return rows[0]["cnt"] if rows else 0
    finally:
        await db.close()


async def get_referral_earnings(user_id: int) -> float:
    db = await get_db()
    try:
        rows = await db.execute_fetchall(
            "SELECT COALESCE(SUM(amount_ton),0) as total FROM referral_payouts WHERE referrer_id=?",
            (user_id,)
        )
        return rows[0]["total"] if rows else 0.0
    finally:
        await db.close()


async def record_referral_payout(referrer_id: int, from_user_id: int,
                                  tx_id: int, amount_ton: float):
    db = await get_db()
    try:
        await db.execute(
            "INSERT INTO referral_payouts (referrer_id, from_user_id, tx_id, amount_ton) VALUES (?,?,?,?)",
            (referrer_id, from_user_id, tx_id, amount_ton)
        )
        await db.commit()
    finally:
        await db.close()


# ── Admin stats ───────────────────────────────────────────────────────────────

async def get_platform_stats() -> dict:
    db = await get_db()
    try:
        users = (await db.execute_fetchall("SELECT COUNT(*) as c FROM users"))[0]["c"]
        listings = (await db.execute_fetchall(
            "SELECT COUNT(*) as c FROM listings WHERE status='active'"))[0]["c"]
        auctions = (await db.execute_fetchall(
            "SELECT COUNT(*) as c FROM auctions WHERE status='active'"))[0]["c"]
        volume_row = await db.execute_fetchall(
            "SELECT COALESCE(SUM(amount_ton),0) as v FROM transactions"
        )
        fees_row = await db.execute_fetchall(
            "SELECT COALESCE(SUM(fee_ton),0) as f FROM transactions"
        )
        return {
            "users": users,
            "active_listings": listings,
            "active_auctions": auctions,
            "total_volume": volume_row[0]["v"],
            "total_fees": fees_row[0]["f"],
        }
    finally:
        await db.close()
