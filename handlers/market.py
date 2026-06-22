from aiogram import Router, F
from aiogram.types import Message, CallbackQuery

from db.queries import (get_active_listings, get_listing, get_user,
                         record_transaction, transfer_gift,
                         increment_views, get_referral_count,
                         record_referral_payout)
from keyboards.keyboards import listings_page_kb, market_listing_kb, buy_confirm_kb
from config import MARKET_FEE, REFERRAL_BONUS_PERCENT, ITEMS_PER_PAGE
from db import queries

router = Router()

RARITY_EMOJI = {"Common": "⬜", "Rare": "🔵", "Epic": "🟣", "Legendary": "🟡"}


def listing_text(lst: dict) -> str:
    re = RARITY_EMOJI.get(lst["rarity"], "⬜")
    seller = f"@{lst['seller_username']}" if lst.get("seller_username") else "Продавец"
    desc = f"\n📝 {lst['description']}" if lst.get("description") else ""
    return (
        f"{re} <b>{lst['gift_name']}</b> #{lst.get('gift_number', '?')}\n"
        f"📦 Коллекция: <b>{lst['collection_name']}</b>\n"
        f"✨ Редкость: <b>{lst['rarity']}</b>\n"
        f"💰 Цена: <b>{lst['price_ton']:.4f} TON</b>\n"
        f"👤 Продавец: {seller}\n"
        f"👁 Просмотров: {lst.get('views', 0)}"
        f"{desc}"
    )


@router.message(F.text == "🛍 Маркет")
async def market_menu(message: Message):
    await show_market(message, offset=0, is_message=True)


@router.callback_query(F.data.startswith("market:"))
async def market_page_cb(callback: CallbackQuery):
    offset = int(callback.data.split(":")[1])
    await show_market(callback.message, offset=offset, is_message=False)
    await callback.answer()


async def show_market(target, offset: int, is_message: bool):
    listings = await get_active_listings(limit=ITEMS_PER_PAGE + 1, offset=offset)
    has_more = len(listings) > ITEMS_PER_PAGE
    listings = listings[:ITEMS_PER_PAGE]

    if not listings:
        text = "😔 Активных объявлений пока нет.\nБудь первым — выставь свой подарок!"
        if is_message:
            await target.answer(text)
        else:
            await target.edit_text(text)
        return

    # Rough total for pagination (cheap approach without COUNT(*))
    total_approx = offset + len(listings) + (1 if has_more else 0)
    kb = listings_page_kb(listings, offset, total_approx)
    header = (
        f"🛍 <b>Маркет</b> — {len(listings)} объявлений на странице\n"
        f"Комиссия: всего <b>{int(MARKET_FEE*100)}%</b> 🔥\n\n"
        f"Нажми на подарок, чтобы подробнее:"
    )
    if is_message:
        await target.answer(header, reply_markup=kb)
    else:
        await target.edit_text(header, reply_markup=kb)


@router.callback_query(F.data.startswith("listing:"))
async def show_listing(callback: CallbackQuery):
    listing_id = int(callback.data.split(":")[1])
    lst = await get_listing(listing_id)
    if not lst or lst["status"] != "active":
        await callback.answer("❌ Объявление уже недоступно", show_alert=True)
        return

    await increment_views(listing_id)
    text = listing_text(lst)
    kb = market_listing_kb(listing_id, lst["seller_id"], callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("buy:"))
async def buy_prompt(callback: CallbackQuery):
    listing_id = int(callback.data.split(":")[1])
    lst = await get_listing(listing_id)
    if not lst or lst["status"] != "active":
        await callback.answer("❌ Объявление недоступно", show_alert=True)
        return
    if lst["seller_id"] == callback.from_user.id:
        await callback.answer("❌ Нельзя купить собственный лот", show_alert=True)
        return

    buyer = await get_user(callback.from_user.id)
    fee = lst["price_ton"] * MARKET_FEE
    total = lst["price_ton"]

    text = (
        f"💳 <b>Подтвердить покупку</b>\n\n"
        f"🎁 {lst['gift_name']} #{lst.get('gift_number','?')}\n"
        f"💰 Цена: <b>{total:.4f} TON</b>\n"
        f"💸 Комиссия сервиса: <b>{fee:.4f} TON ({int(MARKET_FEE*100)}%)</b>\n"
        f"💵 Продавец получит: <b>{(total - fee):.4f} TON</b>\n\n"
        f"⚠️ Убедись, что на балансе достаточно TON."
    )
    await callback.message.edit_text(text, reply_markup=buy_confirm_kb(listing_id))
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_buy:"))
async def confirm_buy(callback: CallbackQuery):
    listing_id = int(callback.data.split(":")[1])
    lst = await get_listing(listing_id)
    if not lst or lst["status"] != "active":
        await callback.answer("❌ Объявление уже недоступно", show_alert=True)
        return

    buyer_id = callback.from_user.id
    seller_id = lst["seller_id"]

    if buyer_id == seller_id:
        await callback.answer("❌ Нельзя купить собственный лот", show_alert=True)
        return

    buyer = await get_user(buyer_id)
    price = lst["price_ton"]
    fee = price * MARKET_FEE

    # Check if seller has referrer → pay bonus
    seller = await get_user(seller_id)
    ref_bonus = 0.0
    if seller and seller.get("referred_by"):
        ref_bonus = price * REFERRAL_BONUS_PERCENT

    # NOTE: In production, integrate real TON payment here.
    # For now, simulate with internal balance (demo mode).
    if buyer["balance_ton"] < price:
        await callback.answer(
            f"❌ Недостаточно средств. Баланс: {buyer['balance_ton']:.4f} TON", show_alert=True
        )
        return

    # Deduct from buyer
    await queries.update_balance(buyer_id, -price)
    # Credit seller (minus fee and ref bonus)
    seller_net = price - fee - ref_bonus
    await queries.update_balance(seller_id, seller_net)

    # Transfer NFT
    await transfer_gift(lst["gift_id"], buyer_id)

    # Mark listing sold
    from db.database import get_db
    async with await get_db() as db:
        await db.execute(
            "UPDATE listings SET status='sold', sold_at=datetime('now') WHERE listing_id=?",
            (listing_id,)
        )
        await db.commit()

    # Record tx
    tx_id = await record_transaction(
        buyer_id, seller_id, lst["gift_id"],
        price, fee, ref_bonus, "listing", listing_id
    )

    # Referral payout
    if ref_bonus > 0 and seller.get("referred_by"):
        await queries.update_balance(seller["referred_by"], ref_bonus)
        await record_referral_payout(seller["referred_by"], seller_id, tx_id, ref_bonus)

    await callback.message.edit_text(
        f"✅ <b>Покупка успешна!</b>\n\n"
        f"🎁 <b>{lst['gift_name']}</b> теперь твой!\n"
        f"Заплачено: {price:.4f} TON\n"
        f"Комиссия: {fee:.4f} TON\n\n"
        f"Смотри подарок в разделе 💼 Портфолио"
    )
    await callback.answer("🎉 Покупка совершена!")

    # Notify seller
    try:
        from aiogram import Bot
        from aiogram.client.default import DefaultBotProperties
        # Bot is available via callback.bot
        buyer_name = callback.from_user.username or callback.from_user.first_name
        await callback.bot.send_message(
            seller_id,
            f"🎉 <b>Ваш подарок продан!</b>\n\n"
            f"🎁 {lst['gift_name']} #{lst.get('gift_number','?')}\n"
            f"💰 Вы получили: {seller_net:.4f} TON\n"
            f"👤 Покупатель: @{buyer_name}"
        )
    except Exception:
        pass
