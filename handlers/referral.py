from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.utils.deep_linking import create_start_link

from db.queries import get_user, get_referral_count, get_referral_earnings
from config import REFERRAL_BONUS_PERCENT

router = Router()


@router.message(F.text == "👥 Рефералы")
async def referral_menu(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала /start")
        return

    ref_count = await get_referral_count(message.from_user.id)
    ref_earned = await get_referral_earnings(message.from_user.id)

    # Generate deep link
    bot_info = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{message.from_user.id}"

    text = (
        f"👥 <b>Реферальная программа</b>\n\n"
        f"Получай <b>{int(REFERRAL_BONUS_PERCENT * 100)}%</b> с каждой продажи "
        f"твоих рефералов — навсегда!\n\n"
        f"📊 <b>Твоя статистика:</b>\n"
        f"├ Приглашено: <b>{ref_count}</b> чел.\n"
        f"└ Заработано: <b>{ref_earned:.4f} TON</b>\n\n"
        f"🔗 <b>Твоя реферальная ссылка:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"💡 Поделись ссылкой с друзьями — они начнут торговать, "
        f"а ты будешь получать пассивный доход!"
    )
    await message.answer(text)
