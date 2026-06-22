from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import CommandStart, Command

from db.queries import get_or_create_user, get_user, get_referral_count, get_referral_earnings
from keyboards.keyboards import main_menu_kb
from config import MARKET_FEE, REFERRAL_BONUS_PERCENT

router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message):
    args = message.text.split()
    referred_by = None

    # Parse referral link: /start ref_12345
    if len(args) > 1 and args[1].startswith("ref_"):
        try:
            ref_id = int(args[1].replace("ref_", ""))
            if ref_id != message.from_user.id:
                referred_by = ref_id
        except ValueError:
            pass

    user = await get_or_create_user(
        user_id=message.from_user.id,
        username=message.from_user.username or "",
        full_name=message.from_user.full_name or "",
        referred_by=referred_by
    )

    welcome = (
        f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\n"
        f"🎁 <b>NFT Gift Market</b> — честный маркетплейс\n"
        f"для обмена Telegram-подарков без казино и обмана.\n\n"
        f"✅ Комиссия всего <b>{int(MARKET_FEE*100)}%</b> — одна из самых низких\n"
        f"✅ Без азартных игр\n"
        f"✅ Реферальный бонус <b>{int(REFERRAL_BONUS_PERCENT*100)}%</b> с каждой продажи\n"
        f"✅ Аукционы и фиксированные цены\n\n"
        f"Выбери раздел ниже 👇"
    )
    await message.answer(welcome, reply_markup=main_menu_kb())


@router.message(F.text == "👤 Профиль")
@router.message(Command("profile"))
async def cmd_profile(message: Message):
    user = await get_user(message.from_user.id)
    if not user:
        await message.answer("Сначала запусти бота: /start")
        return

    ref_count = await get_referral_count(message.from_user.id)
    ref_earned = await get_referral_earnings(message.from_user.id)
    username_str = f"@{user['username']}" if user["username"] else "не задан"

    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"📛 Username: {username_str}\n"
        f"💰 Баланс: <b>{user['balance_ton']:.4f} TON</b>\n\n"
        f"📈 <b>Статистика</b>\n"
        f"├ Потрачено: {user['total_spent']:.4f} TON\n"
        f"├ Заработано: {user['total_earned']:.4f} TON\n"
        f"├ Рефералы: {ref_count} чел.\n"
        f"└ Заработано на рефералах: {ref_earned:.4f} TON\n\n"
        f"📅 Зарегистрирован: {user['joined_at'][:10]}"
    )
    await message.answer(text)
