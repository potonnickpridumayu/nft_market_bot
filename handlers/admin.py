from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from db.queries import get_platform_stats, add_gift, get_user
from keyboards.keyboards import admin_kb
from config import ADMIN_IDS

router = Router()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


class AdminStates(StatesGroup):
    ban_user_id    = State()
    add_gift_uid   = State()
    add_gift_coll  = State()
    add_gift_name  = State()
    add_gift_num   = State()
    add_gift_rarity = State()


@router.message(Command("admin"))
async def admin_panel(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🔧 <b>Админ-панель</b>", reply_markup=admin_kb())


@router.callback_query(F.data == "admin_stats")
async def admin_stats(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("❌ Нет прав", show_alert=True)
        return

    stats = await get_platform_stats()
    text = (
        f"📊 <b>Статистика платформы</b>\n\n"
        f"👥 Пользователей: <b>{stats['users']}</b>\n"
        f"🛍 Активных объявлений: <b>{stats['active_listings']}</b>\n"
        f"🔨 Активных аукционов: <b>{stats['active_auctions']}</b>\n"
        f"💰 Общий объём: <b>{stats['total_volume']:.4f} TON</b>\n"
        f"💸 Заработано комиссий: <b>{stats['total_fees']:.4f} TON</b>"
    )
    await callback.message.edit_text(text, reply_markup=admin_kb())
    await callback.answer()


@router.callback_query(F.data == "admin_ban")
async def admin_ban_prompt(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.answer("🚫 Введи user_id для бана:")
    await state.set_state(AdminStates.ban_user_id)
    await callback.answer()


@router.message(AdminStates.ban_user_id)
async def admin_ban_exec(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        target_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Неверный ID")
        return

    from db.database import get_db
    async with await get_db() as db:
        await db.execute("UPDATE users SET is_banned=1 WHERE user_id=?", (target_id,))
        await db.commit()

    await state.clear()
    await message.answer(f"🚫 Пользователь {target_id} заблокирован.")


@router.callback_query(F.data == "admin_add_gift")
async def admin_add_gift_start(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return
    await callback.message.answer("🎁 ID пользователя для выдачи подарка:")
    await state.set_state(AdminStates.add_gift_uid)
    await callback.answer()


@router.message(AdminStates.add_gift_uid)
async def admin_gift_uid(message: Message, state: FSMContext):
    try:
        uid = int(message.text.strip())
        user = await get_user(uid)
        if not user:
            await message.answer("❌ Пользователь не найден")
            return
        await state.update_data(target_uid=uid)
        await message.answer(f"👤 Выдаём подарок @{user.get('username','?')}\n\nНазвание коллекции:")
        await state.set_state(AdminStates.add_gift_coll)
    except ValueError:
        await message.answer("❌ Введи числовой ID")


@router.message(AdminStates.add_gift_coll)
async def admin_gift_coll(message: Message, state: FSMContext):
    await state.update_data(collection=message.text.strip())
    await message.answer("Название подарка:")
    await state.set_state(AdminStates.add_gift_name)


@router.message(AdminStates.add_gift_name)
async def admin_gift_name(message: Message, state: FSMContext):
    await state.update_data(gift_name=message.text.strip())
    await message.answer("Номер (#1234) или /skip:")
    await state.set_state(AdminStates.add_gift_num)


@router.message(AdminStates.add_gift_num)
async def admin_gift_num(message: Message, state: FSMContext):
    num = "" if message.text.strip() == "/skip" else message.text.strip()
    await state.update_data(gift_number=num)
    await message.answer("Редкость (Common/Rare/Epic/Legendary):")
    await state.set_state(AdminStates.add_gift_rarity)


@router.message(AdminStates.add_gift_rarity)
async def admin_gift_rarity(message: Message, state: FSMContext):
    rarity = message.text.strip().capitalize()
    if rarity not in ("Common", "Rare", "Epic", "Legendary"):
        await message.answer("❌ Выбери: Common, Rare, Epic, Legendary")
        return

    data = await state.get_data()
    await state.clear()

    gift_id = await add_gift(
        owner_id=data["target_uid"],
        collection_name=data["collection"],
        gift_name=data["gift_name"],
        gift_number=data.get("gift_number", ""),
        rarity=rarity
    )

    await message.answer(
        f"✅ Подарок выдан!\n"
        f"🎁 {data['gift_name']} #{data.get('gift_number','?')}\n"
        f"👤 Пользователю: {data['target_uid']}\n"
        f"🆔 gift_id: {gift_id}"
    )

    try:
        await message.bot.send_message(
            data["target_uid"],
            f"🎁 <b>Вам выдан подарок!</b>\n\n"
            f"✨ {data['gift_name']} ({rarity})\n"
            f"Смотри в 💼 Портфолио!"
        )
    except Exception:
        pass
