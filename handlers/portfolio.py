from aiogram import Router, F
from aiogram.types import Message, CallbackQuery

from db.queries import get_user_gifts, get_gift, get_user_transactions
from keyboards.keyboards import portfolio_kb, gift_detail_kb
from config import ITEMS_PER_PAGE

router = Router()

RARITY_EMOJI = {"Common": "⬜", "Rare": "🔵", "Epic": "🟣", "Legendary": "🟡"}


@router.message(F.text == "💼 Портфолио")
async def portfolio_menu(message: Message):
    gifts = await get_user_gifts(message.from_user.id)
    if not gifts:
        await message.answer(
            "💼 <b>Твоё портфолио пусто</b>\n\n"
            "Купи подарки на 🛍 Маркете или выиграй на 🔨 Аукционе!"
        )
        return

    # Stats
    rarity_count: dict = {}
    for g in gifts:
        r = g.get("rarity", "Common")
        rarity_count[r] = rarity_count.get(r, 0) + 1

    stats_lines = " | ".join(
        f"{RARITY_EMOJI.get(r,'?')} {r}: {c}"
        for r, c in sorted(rarity_count.items())
    )
    text = (
        f"💼 <b>Портфолио</b> — {len(gifts)} подарков\n"
        f"{stats_lines}\n\n"
        f"Выбери подарок:"
    )
    kb = portfolio_kb(gifts, offset=0)
    await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("portfolio:"))
async def portfolio_page(callback: CallbackQuery):
    offset = int(callback.data.split(":")[1])
    gifts = await get_user_gifts(callback.from_user.id)
    if not gifts:
        await callback.message.edit_text("💼 Портфолио пусто.")
        await callback.answer()
        return
    kb = portfolio_kb(gifts, offset=offset)
    await callback.message.edit_text(
        f"💼 <b>Портфолио</b> — {len(gifts)} подарков\nВыбери подарок:",
        reply_markup=kb
    )
    await callback.answer()


@router.callback_query(F.data.startswith("gift_detail:"))
async def gift_detail(callback: CallbackQuery):
    gift_id = int(callback.data.split(":")[1])
    gift = await get_gift(gift_id)
    if not gift:
        await callback.answer("❌ Подарок не найден", show_alert=True)
        return

    re = RARITY_EMOJI.get(gift["rarity"], "⬜")
    text = (
        f"{re} <b>{gift['gift_name']}</b> #{gift.get('gift_number','?')}\n\n"
        f"📦 Коллекция: <b>{gift['collection_name']}</b>\n"
        f"✨ Редкость: <b>{gift['rarity']}</b>\n"
        f"📅 Получен: {gift['acquired_at'][:10]}\n"
    )
    if gift.get("nft_address"):
        text += f"🔗 NFT: <code>{gift['nft_address']}</code>\n"

    kb = gift_detail_kb(gift_id, gift["owner_id"], callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


# ── Transaction history ───────────────────────────────────────────────────────

@router.message(F.text == "📜 История")
async def tx_history(message: Message):
    txs = await get_user_transactions(message.from_user.id, limit=10)
    if not txs:
        await message.answer("📜 История сделок пуста.")
        return

    lines = []
    for tx in txs:
        action = "🛍 Покупка" if tx["buyer_id"] == message.from_user.id else "💰 Продажа"
        lines.append(
            f"{action} — <b>{tx['gift_name']}</b>\n"
            f"   💰 {tx['amount_ton']:.4f} TON | "
            f"{'💸' if tx['buyer_id'] == message.from_user.id else '💵'} "
            f"{tx['completed_at'][:10]}"
        )

    await message.answer(
        f"📜 <b>История сделок</b> (последние {len(txs)}):\n\n" + "\n\n".join(lines)
    )
