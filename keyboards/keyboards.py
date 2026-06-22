"""All inline / reply keyboards."""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from config import ITEMS_PER_PAGE


# ── Main menu ────────────────────────────────────────────────────────────────

def main_menu_kb() -> ReplyKeyboardMarkup:
    builder = ReplyKeyboardBuilder()
    builder.row(
        KeyboardButton(text="🛍 Маркет"),
        KeyboardButton(text="🔨 Аукционы"),
    )
    builder.row(
        KeyboardButton(text="💼 Портфолио"),
        KeyboardButton(text="➕ Продать"),
    )
    builder.row(
        KeyboardButton(text="👥 Рефералы"),
        KeyboardButton(text="👤 Профиль"),
    )
    return builder.as_markup(resize_keyboard=True)


# ── Market ───────────────────────────────────────────────────────────────────

def market_listing_kb(listing_id: int, seller_id: int, viewer_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if viewer_id != seller_id:
        builder.button(text="💳 Купить", callback_data=f"buy:{listing_id}")
    else:
        builder.button(text="❌ Снять с продажи", callback_data=f"cancel_listing:{listing_id}")
    builder.button(text="◀ Назад", callback_data="market:0")
    builder.adjust(1)
    return builder.as_markup()


def listings_page_kb(listings: list, offset: int, total: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for lst in listings:
        rarity_emoji = {"Common": "⬜", "Rare": "🔵", "Epic": "🟣", "Legendary": "🟡"}.get(
            lst["rarity"], "⬜"
        )
        builder.button(
            text=f"{rarity_emoji} {lst['gift_name']} #{lst.get('gift_number','?')} — {lst['price_ton']:.2f} TON",
            callback_data=f"listing:{lst['listing_id']}"
        )
    # Pagination
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"market:{offset - ITEMS_PER_PAGE}"))
    if offset + ITEMS_PER_PAGE < total:
        nav.append(InlineKeyboardButton(text="Вперёд ▶", callback_data=f"market:{offset + ITEMS_PER_PAGE}"))
    if nav:
        builder.row(*nav)
    builder.adjust(1)
    return builder.as_markup()


def buy_confirm_kb(listing_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить покупку", callback_data=f"confirm_buy:{listing_id}")
    builder.button(text="❌ Отмена", callback_data=f"listing:{listing_id}")
    builder.adjust(1)
    return builder.as_markup()


# ── Auction ───────────────────────────────────────────────────────────────────

def auctions_page_kb(auctions: list, offset: int, total: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for a in auctions:
        builder.button(
            text=f"🔨 {a['gift_name']} — {a['current_price']:.2f} TON",
            callback_data=f"auction:{a['auction_id']}"
        )
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"auctions:{offset - ITEMS_PER_PAGE}"))
    if offset + ITEMS_PER_PAGE < total:
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"auctions:{offset + ITEMS_PER_PAGE}"))
    if nav:
        builder.row(*nav)
    builder.adjust(1)
    return builder.as_markup()


def auction_kb(auction_id: int, seller_id: int, viewer_id: int,
               buyout_price=None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if viewer_id != seller_id:
        builder.button(text="⬆ Сделать ставку", callback_data=f"bid:{auction_id}")
        if buyout_price:
            builder.button(text=f"⚡ Купить сразу ({buyout_price:.2f} TON)",
                           callback_data=f"buyout:{auction_id}")
    else:
        builder.button(text="❌ Отменить аукцион", callback_data=f"cancel_auction:{auction_id}")
    builder.button(text="◀ Назад", callback_data="auctions:0")
    builder.adjust(1)
    return builder.as_markup()


# ── Sell ──────────────────────────────────────────────────────────────────────

def sell_type_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🏷 Фиксированная цена", callback_data="sell_type:fixed")
    builder.button(text="🔨 Аукцион", callback_data="sell_type:auction")
    builder.button(text="❌ Отмена", callback_data="cancel_sell")
    builder.adjust(1)
    return builder.as_markup()


def gift_select_kb(gifts: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for g in gifts:
        rarity_emoji = {"Common": "⬜", "Rare": "🔵", "Epic": "🟣", "Legendary": "🟡"}.get(
            g["rarity"], "⬜"
        )
        builder.button(
            text=f"{rarity_emoji} {g['gift_name']} #{g.get('gift_number','?')}",
            callback_data=f"select_gift:{g['gift_id']}"
        )
    builder.button(text="❌ Отмена", callback_data="cancel_sell")
    builder.adjust(1)
    return builder.as_markup()


def confirm_sell_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выставить", callback_data="confirm_sell")
    builder.button(text="❌ Отмена", callback_data="cancel_sell")
    builder.adjust(2)
    return builder.as_markup()


# ── Portfolio ─────────────────────────────────────────────────────────────────

def portfolio_kb(gifts: list, offset: int = 0) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    page = gifts[offset:offset + ITEMS_PER_PAGE]
    for g in page:
        rarity_emoji = {"Common": "⬜", "Rare": "🔵", "Epic": "🟣", "Legendary": "🟡"}.get(
            g["rarity"], "⬜"
        )
        builder.button(
            text=f"{rarity_emoji} {g['gift_name']} #{g.get('gift_number','?')}",
            callback_data=f"gift_detail:{g['gift_id']}"
        )
    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀", callback_data=f"portfolio:{offset - ITEMS_PER_PAGE}"))
    if offset + ITEMS_PER_PAGE < len(gifts):
        nav.append(InlineKeyboardButton(text="▶", callback_data=f"portfolio:{offset + ITEMS_PER_PAGE}"))
    if nav:
        builder.row(*nav)
    builder.adjust(1)
    return builder.as_markup()


def gift_detail_kb(gift_id: int, owner_id: int, viewer_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if owner_id == viewer_id:
        builder.button(text="📤 Продать", callback_data=f"quick_sell:{gift_id}")
    builder.button(text="◀ Назад", callback_data="portfolio:0")
    builder.adjust(1)
    return builder.as_markup()


# ── Admin ─────────────────────────────────────────────────────────────────────

def admin_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="📊 Статистика", callback_data="admin_stats")
    builder.button(text="🚫 Забанить пользователя", callback_data="admin_ban")
    builder.button(text="🎁 Добавить NFT пользователю", callback_data="admin_add_gift")
    builder.adjust(1)
    return builder.as_markup()
