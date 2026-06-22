import asyncio
import logging
from db.queries import (transfer_gift, record_transaction,
                        update_balance, record_referral_payout, get_user)
from config import MARKET_FEE, REFERRAL_BONUS_PERCENT
import aiosqlite

logger = logging.getLogger(__name__)
DB_PATH = "nft_market.db"


async def close_expired_auctions(bot):
    try:
        db = await aiosqlite.connect(DB_PATH)
        db.row_factory = aiosqlite.Row
        try:
            expired = await db.execute_fetchall(
                """SELECT a.*, g.gift_name, g.gift_number, g.gift_id as gid
                   FROM auctions a
                   JOIN gifts g ON a.gift_id=g.gift_id
                   WHERE a.status='active' AND a.ends_at <= datetime('now')"""
            )
        finally:
            await db.close()

        for row in expired:
            a = dict(row)
            auction_id = a["auction_id"]

            if not a.get("current_bidder"):
                db2 = await aiosqlite.connect(DB_PATH)
                try:
                    await db2.execute(
                        "UPDATE auctions SET status='ended' WHERE auction_id=?", (auction_id,)
                    )
                    await db2.commit()
                finally:
                    await db2.close()
                try:
                    await bot.send_message(
                        a["seller_id"],
                        f"Аукцион завершён без ставок\n"
                        f"Подарок {a['gift_name']} остался у тебя."
                    )
                except Exception:
                    pass
                continue

            buyer_id = a["current_bidder"]
            seller_id = a["seller_id"]
            price = a["current_price"]
            fee = price * MARKET_FEE

            seller = await get_user(seller_id)
            ref_bonus = 0.0
            if seller and seller.get("referred_by"):
                ref_bonus = price * REFERRAL_BONUS_PERCENT

            seller_net = price - fee - ref_bonus
            await update_balance(seller_id, seller_net)
            await transfer_gift(a["gift_id"], buyer_id)

            db3 = await aiosqlite.connect(DB_PATH)
            try:
                await db3.execute(
                    "UPDATE auctions SET status='ended' WHERE auction_id=?", (auction_id,)
                )
                await db3.commit()
            finally:
                await db3.close()

            tx_id = await record_transaction(
                buyer_id, seller_id, a["gift_id"],
                price, fee, ref_bonus, "auction", auction_id
            )

            if ref_bonus > 0 and seller and seller.get("referred_by"):
                await update_balance(seller["referred_by"], ref_bonus)
                await record_referral_payout(seller["referred_by"], seller_id, tx_id, ref_bonus)

            try:
                await bot.send_message(buyer_id, f"Вы выиграли аукцион! Подарок {a['gift_name']} ваш.")
            except Exception:
                pass
            try:
                await bot.send_message(seller_id, f"Аукцион завершён. Вы получили {seller_net:.4f} TON.")
            except Exception:
                pass

    except Exception as e:
        logger.error(f"Auction scheduler error: {e}")


async def run_scheduler(bot):
    logger.info("Auction scheduler started")
    while True:
        await asyncio.sleep(60)
        await close_expired_auctions(bot)
