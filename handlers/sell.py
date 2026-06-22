from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db.queries import get_user_gifts, create_listing, add_gift
from keyboards.keyboards import sell_type_kb, gift_select_kb, confirm_sell_kb
from config import MIN_PRICE_TON, MARKET_FEE

router = Router()


class SellStates(StatesGroup):
    choose_type      = State()
    choose_gift      = State()
    enter_price      = State()
    enter_desc       = State()
    confirm          = State()
    enter_min_step   = State()
    enter_buyout     = State()
    enter_duration   = State()


# ── Entry point ──────────────────────────────────────────────────────────────

@router.message(F.text == "➕ Продать")
async def sell_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "📤 <b>Выставить подарок на продажу</b>\n\nВыбери тип продажи:",
        reply_markup=sell_type_kb()
    )
    await state.set_state(SellStates.choose_type)


@router.callback_query(F.data == "cancel_sell")
async def cancel_sell(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌ Отменено.")
    await callback.answer()


# ── Choose sell type ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sell_type:"), SellStates.choose_type)
async def choose_sell_type(callback: CallbackQuery, state: FSMContext):
    sell_type = callback.data.split(":")[1]
    await state.update_data(sell_type=sell_type)

    gifts = await get_user_gifts(callback.from_user.id)
    if not gifts:
        await callback.message.edit_text(
            "😔 У тебя нет подарков для продажи.\n"
            "Получи NFT-подарки в Telegram и возвращайся!"
        )
        await state.clear()
        await callback.answer()
        return

    await callback.message.edit_text(
        "🎁 Выбери подарок для продажи:",
        reply_markup=gift_select_kb(gifts)
    )
    await state.set_state(SellStates.choose_gift)
    await callback.answer()


# ── Choose gift ───────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("select_gift:"), SellStates.choose_gift)
async def select_gift(callback: CallbackQuery, state: FSMContext):
    gift_id = int(callback.data.split(":")[1])
    await state.update_data(gift_id=gift_id)
    data = await state.get_data()

    if data["sell_type"] == "fixed":
        await callback.message.edit_text(
            f"💰 Укажи цену в TON (минимум {MIN_PRICE_TON} TON):\n"
            f"Пример: <code>5.5</code>"
        )
        await state.set_state(SellStates.enter_price)
    else:
        await callback.message.edit_text(
            f"💰 Стартовая цена аукциона в TON (минимум {MIN_PRICE_TON} TON):\n"
            f"Пример: <code>2.0</code>"
        )
        await state.set_state(SellStates.enter_price)
    await callback.answer()


# ── Quick sell from portfolio ─────────────────────────────────────────────────

@router.callback_query(F.data.startswith("quick_sell:"))
async def quick_sell(callback: CallbackQuery, state: FSMContext):
    gift_id = int(callback.data.split(":")[1])
    await state.clear()
    await state.update_data(gift_id=gift_id, sell_type="fixed")
    await callback.message.edit_text(
        f"💰 Укажи цену в TON (минимум {MIN_PRICE_TON} TON):\n"
        f"Пример: <code>5.5</code>"
    )
    await state.set_state(SellStates.enter_price)
    await callback.answer()


# ── Enter price ───────────────────────────────────────────────────────────────

@router.message(SellStates.enter_price)
async def enter_price(message: Message, state: FSMContext):
    try:
        price = float(message.text.strip().replace(",", "."))
    except ValueError:
        await message.answer("❌ Введи число. Например: <code>3.5</code>")
        return

    if price < MIN_PRICE_TON:
        await message.answer(f"❌ Минимальная цена: {MIN_PRICE_TON} TON")
        return

    await state.update_data(price=price)
    data = await state.get_data()

    if data["sell_type"] == "auction":
        await message.answer(
            "⬆ Минимальный шаг ставки в TON (например <code>0.5</code>).\n"
            "Или отправь <code>0</code> для шага 5% от текущей цены:"
        )
        await state.set_state(SellStates.enter_min_step)
    else:
        await message.answer(
            "📝 Добавь описание подарка (или /skip чтобы пропустить):"
        )
        await state.set_state(SellStates.enter_desc)


# ── Auction: min step ─────────────────────────────────────────────────────────

@router.message(SellStates.enter_min_step)
async def enter_min_step(message: Message, state: FSMContext):
    try:
        step = float(message.text.strip().replace(",", "."))
        if step < 0:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи число >= 0. Например: <code>0.5</code>")
        return

    data = await state.get_data()
    if step == 0:
        step = data["price"] * 0.05

    await state.update_data(min_step=step)
    await message.answer(
        "⚡ Цена мгновенной покупки (buyout) в TON.\n"
        "Или /skip, чтобы без buyout:"
    )
    await state.set_state(SellStates.enter_buyout)


@router.message(SellStates.enter_buyout)
async def enter_buyout(message: Message, state: FSMContext):
    buyout = None
    if message.text.strip() != "/skip":
        try:
            buyout = float(message.text.strip().replace(",", "."))
            data = await state.get_data()
            if buyout <= data["price"]:
                await message.answer("❌ Buyout должен быть выше стартовой цены.")
                return
        except ValueError:
            await message.answer("❌ Введи число или /skip")
            return

    await state.update_data(buyout=buyout)
    await message.answer(
        "⏱ Длительность аукциона в часах (например <code>24</code>, макс 168):"
    )
    await state.set_state(SellStates.enter_duration)


@router.message(SellStates.enter_duration)
async def enter_duration(message: Message, state: FSMContext):
    try:
        hours = int(message.text.strip())
        if hours < 1 or hours > 168:
            raise ValueError
    except ValueError:
        await message.answer("❌ Введи число от 1 до 168 (часы).")
        return

    await state.update_data(duration_hours=hours)
    await message.answer("📝 Добавь описание (или /skip):")
    await state.set_state(SellStates.enter_desc)


# ── Description & confirm ─────────────────────────────────────────────────────

@router.message(SellStates.enter_desc)
async def enter_desc(message: Message, state: FSMContext):
    desc = "" if message.text.strip() == "/skip" else message.text.strip()[:300]
    await state.update_data(description=desc)
    data = await state.get_data()

    fee = data["price"] * MARKET_FEE
    fee_pct = int(MARKET_FEE * 100)
    net = data["price"] - fee

    if data["sell_type"] == "fixed":
        summary = (
            "📋 <b>Проверь объявление:</b>\n\n"
            "🏷 Тип: Фиксированная цена\n"
            f"💰 Цена: <b>{data['price']:.4f} TON</b>\n"
            f"💸 Комиссия ({fee_pct}%): {fee:.4f} TON\n"
            f"💵 Получишь: <b>{net:.4f} TON</b>\n"
        )
    else:
        buyout_val = data.get("buyout")
        buyout_str = f"{buyout_val:.4f} TON" if buyout_val else "нет"
        summary = (
            "📋 <b>Проверь аукцион:</b>\n\n"
            "🔨 Тип: Аукцион\n"
            f"💰 Старт: <b>{data['price']:.4f} TON</b>\n"
            f"⬆ Мин. шаг: {data.get('min_step', 0):.4f} TON\n"
            f"⚡ Buyout: {buyout_str}\n"
            f"⏱ Длительность: {data.get('duration_hours', 24)} ч.\n"
            f"💸 Комиссия: {fee_pct}%\n"
        )
    if desc:
        summary += f"📝 Описание: {desc}\n"

    summary += "\n✅ Всё верно?"
    await message.answer(summary, reply_markup=confirm_sell_kb())
    await state.set_state(SellStates.confirm)


@router.callback_query(F.data == "confirm_sell", SellStates.confirm)
async def confirm_sell(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()

    gift_id = data["gift_id"]

    if data["sell_type"] == "fixed":
        listing_id = await create_listing(
            gift_id=gift_id,
            seller_id=callback.from_user.id,
            price_ton=data["price"],
            description=data.get("description", "")
        )
        await callback.message.edit_text(
            f"🎉 <b>Подарок выставлен!</b>\n\n"
            f"🆔 Объявление #{listing_id}\n"
            f"💰 Цена: {data['price']:.4f} TON\n\n"
            f"Оно появится в разделе 🛍 Маркет."
        )
    else:
        from datetime import datetime, timedelta
        from db.queries import create_auction
        hours = data.get("duration_hours", 24)
        ends_at = (datetime.utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
        auction_id = await create_auction(
            gift_id=gift_id,
            seller_id=callback.from_user.id,
            start_price=data["price"],
            min_step=data.get("min_step", data["price"] * 0.05),
            buyout_price=data.get("buyout"),
            ends_at=ends_at
        )
        await callback.message.edit_text(
            f"🎉 <b>Аукцион создан!</b>\n\n"
            f"🆔 Аукцион #{auction_id}\n"
            f"💰 Старт: {data['price']:.4f} TON\n"
            f"⏱ Завершится: {ends_at} UTC\n\n"
            f"Он появится в разделе 🔨 Аукционы."
        )
    await callback.answer("✅ Готово!")
