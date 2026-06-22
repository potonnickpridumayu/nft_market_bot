from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db.queries import (get_active_auctions, get_auction, place_bid,
                         end_auction, transfer_gift, record_transaction,
                         get_user, record_referral_payout, update_balance)
from keyboards.keyboards import auctions_page_kb, auction_kb
from config import MARKET_FEE, REFERRAL_BONUS_PERCENT, ITEMS_PER_PAGE

router = Router()

RARITY_EMOJI = {"Common": "⬜", "Rare": "🔵", "Epic": "🟣", "Legendary": "🟡"}


class BidStates(StatesGroup):
    enter_bid = State()


def auction_text(a: dict) -> str:
    re = RARITY_EMOJI.get(a["rarity"], "⬜")
    from datetime import datetime
    ends_at = a.get("ends_at", "?")
    try:
        dt = datetime.strptime(ends_at, "%Y-%m-%d %H:%M:%S")
        now = datetime.utcnow()
        diff = dt - now
        if diff.total_seconds() > 0:
            h, rem = divmod(int(diff.total_seconds()), 3600)
            m = rem // 60
            time_str = f"{h}ч {m}мин"
        else:
            time_str = "⌛ Завершён"
    except Exception:
        time_str = ends_at

    buyout_str = f"\n⚡ Buyout: <b>{a['buyout_price']:.4f} TON</b>" if a.get("buyout_price") else ""
    bidder_str = f"@{a['current_bidder']}" if a.get("current_bidder") else "нет"

    return (
        f"{re} <b>{a['gift_name']}</b> #{a.get('gift_number','?')}\n"
        f"📦 Коллекция: <b>{a['collection_name']}</b>\n"
        f"✨ Редкость: <b>{a['rarity']}</b>\n\n"
        f"💰 Текущая ставка: <b>{a['current_price']:.4f} TON</b>\n"
        f"⬆ Мин. шаг: {a['min_step']:.4f} TON"
        f"{buyout_str}\n"
        f"👤 Лидирует: {bidder_str}\n"
        f"⏰ До конца: <b>{time_str}</b>"
    )


@router.message(F.text == "🔨 Аукционы")
async def auctions_menu(message: Message):
    await show_auctions(message, offset=0, is_message=True)


@router.callback_query(F.data.startswith("auctions:"))
async def auctions_page_cb(callback: CallbackQuery):
    offset = int(callback.data.split(":")[1])
    await show_auctions(callback.message, offset=offset, is_message=False)
    await callback.answer()


async def show_auctions(target, offset: int, is_message: bool):
    auctions = await get_active_auctions(limit=ITEMS_PER_PAGE + 1, offset=offset)
    has_more = len(auctions) > ITEMS_PER_PAGE
    auctions = auctions[:ITEMS_PER_PAGE]

    if not auctions:
        text = "😔 Активных аукционов нет.\nВыставь свой подарок через ➕ Продать!"
        if is_message:
            await target.answer(text)
        else:
            await target.edit_text(text)
        return

    total_approx = offset + len(auctions) + (1 if has_more else 0)
    kb = auctions_page_kb(auctions, offset, total_approx)
    header = "🔨 <b>Аукционы</b> — актуальные лоты:\n\nНажми на лот для деталей:"
    if is_message:
        await target.answer(header, reply_markup=kb)
    else:
        await target.edit_text(header, reply_markup=kb)


@router.callback_query(F.data.startswith("auction:"))
async def show_auction(callback: CallbackQuery):
    auction_id = int(callback.data.split(":")[1])
    a = await get_auction(auction_id)
    if not a:
        await callback.answer("❌ Аукцион не найден", show_alert=True)
        return

    text = auction_text(a)
    kb = auction_kb(auction_id, a["seller_id"], callback.from_user.id, a.get("buyout_price"))
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# ── Place bid ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("bid:"))
async def bid_prompt(callback: CallbackQuery, state: FSMContext):
    auction_id = int(callback.data.split(":")[1])
    a = await get_auction(auction_id)
    if not a or a["status"] != "active":
        await callback.answer("❌ Аукцион недоступен", show_alert=True)
        return
    if a["seller_id"] == callback.from_user.id:
        await callback.answer("❌ Нельзя ставить на свой аукцион", show_alert=True)
        return

    min_bid = a["current_price"] + a["min_step"]
    await state.update_data(auction_id=auction_id, min_bid=min_bid)
    await callback.message.answer(
        f"⬆ <b>Введи ставку</b>\n\n"
        f"Текущая цена: {a['current_price']:.4f} TON\n"
        f"Минимальная ставка: <b>{min_bid:.4f} TON</b>\n\n"
        f"Введи сумму (TON):"
    )
    await state.set_state(BidStates.enter_bid)
    await callback.answer()


@router.message(BidStates.enter_bid)
async def process_bid(message: Message, state: FSMContext):
    data = await state.get_data()
    try:
        amount = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("❌ Введи корректное число")
        return

    if amount < data["min_bid"]:
        await message.answer(f"❌ Ставка должна быть минимум {data['min_bid']:.4f} TON")
        return

    user = await get_user(message.from_user.id)
    if user["balance_ton"] < amount:
        await message.answer(
            f"❌ Недостаточно средств.\nБаланс: {user['balance_ton']:.4f} TON"
        )
        return

    success = await place_bid(data["auction_id"], message.from_user.id, amount)
    if not success:
        await message.answer("❌ Не удалось принять ставку. Аукцион мог завершиться.")
        await state.clear()
        return

    await state.clear()

    # Lock funds (in production: escrow system)
    await update_balance(message.from_user.id, -amount)

    a = await get_auction(data["auction_id"])
    await message.answer(
        f"✅ <b>Ставка принята!</b>\n\n"
        f"🔨 {a['gift_name']} #{a.get('gift_number','?')}\n"
        f"💰 Ваша ставка: <b>{amount:.4f} TON</b>\n\n"
        f"Уведомим если тебя перебьют!"
    )

    # Notify previous bidder (if any, return their funds)
    # In production: implement proper escrow with notifications


# ── Buyout ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("buyout:"))
async def instant_buyout(callback: CallbackQuery):
    auction_id = int(callback.data.split(":")[1])
    a = await get_auction(auction_id)
    if not a or a["status"] != "active" or not a.get("buyout_price"):
        await callback.answer("❌ Недоступно", show_alert=True)
        return

    buyer_id = callback.from_user.id
    if a["seller_id"] == buyer_id:
        await callback.answer("❌ Нельзя купить собственный лот", show_alert=True)
        return

    buyer = await get_user(buyer_id)
    price = a["buyout_price"]
    fee = price * MARKET_FEE

    if buyer["balance_ton"] < price:
        await callback.answer(
            f"❌ Недостаточно средств. Баланс: {buyer['balance_ton']:.4f} TON",
            show_alert=True
        )
        return

    seller = await get_user(a["seller_id"])
    ref_bonus = 0.0
    if seller and seller.get("referred_by"):
        ref_bonus = price * REFERRAL_BONUS_PERCENT

    seller_net = price - fee - ref_bonus
    await update_balance(buyer_id, -price)
    await update_balance(a["seller_id"], seller_net)
    await transfer_gift(a["gift_id"], buyer_id)
    await end_auction(auction_id)

    tx_id = await record_transaction(
        buyer_id, a["seller_id"], a["gift_id"],
        price, fee, ref_bonus, "auction", auction_id
    )

    if ref_bonus > 0 and seller.get("referred_by"):
        await update_balance(seller["referred_by"], ref_bonus)
        await record_referral_payout(seller["referred_by"], a["seller_id"], tx_id, ref_bonus)

    await callback.message.edit_text(
        f"🎉 <b>Мгновенная покупка!</b>\n\n"
        f"🎁 <b>{a['gift_name']}</b> теперь твой!\n"
        f"💰 Заплачено: {price:.4f} TON\n\n"
        f"Смотри в 💼 Портфолио"
    )
    await callback.answer("🎉 Куплено!")

    try:
        buyer_name = callback.from_user.username or callback.from_user.first_name
        await callback.bot.send_message(
            a["seller_id"],
            f"⚡ <b>Buyout! Твой аукцион завершён</b>\n\n"
            f"🎁 {a['gift_name']} продан за {price:.4f} TON\n"
            f"💵 Ты получил: {seller_net:.4f} TON\n"
            f"👤 Покупатель: @{buyer_name}"
        )
    except Exception:
        pass


# ── Cancel auction ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("cancel_auction:"))
async def cancel_auction_cb(callback: CallbackQuery):
    auction_id = int(callback.data.split(":")[1])
    a = await get_auction(auction_id)
    if not a:
        await callback.answer("❌ Не найден", show_alert=True)
        return
    if a["seller_id"] != callback.from_user.id:
        await callback.answer("❌ Нет прав", show_alert=True)
        return
    if a.get("current_bidder"):
        await callback.answer(
            "❌ Нельзя отменить аукцион с активными ставками!", show_alert=True
        )
        return

    from db.database import get_db
    async with await get_db() as db:
        await db.execute(
            "UPDATE auctions SET status='cancelled' WHERE auction_id=?", (auction_id,)
        )
        await db.commit()

    await callback.message.edit_text("✅ Аукцион отменён.")
    await callback.answer()
