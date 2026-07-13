"""FastAPI сервер для GiftSafe Mini App"""
import hmac
import hashlib
import json
import os
import re
import asyncio
import ton_client
import deposits
import logging
import urllib.request
from typing import Optional, List
from urllib.parse import parse_qsl

from contextlib import asynccontextmanager
from db.queries import init_db, close_pool, get_gift, get_deposit_source, set_listing_status, release_gift, set_gift_owner, gift_is_locked, delete_gift, get_gift_locks
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv

from db.queries import (
    get_active_listings, get_listing, get_active_auctions,
    get_auction, place_bid, get_user_gifts, get_user,
    get_or_create_user, create_listing, add_gift,
    get_user_transactions, get_platform_stats,
    # для покупки:
    update_balance, transfer_gift, record_transaction,
    record_referral_payout, mark_listing_sold,get_or_create_deposit_intent, get_latest_intent_for_user,
    # C-4: надёжные выводы TON:
    create_withdrawal, mark_withdrawal_sent,
    # гард от дублей лотов:
    get_active_listing_for_gift, get_referral_stats,
    # отмена зависшего лота (админ):
    cancel_listing,
    # смена цены лота:
    set_listing_price,
    # админ-ручки:
    get_pool,
    # комиссия за вывод гифта:
    try_charge_balance,
    # обмен:
    create_trade_listing, get_active_trade_listings, get_trade_listing,
    get_active_trade_listing_for_gift, cancel_trade_listing,
    create_trade_offer, get_trade_offer, get_user_trade_offers,
    decline_trade_offer, cancel_trade_offer, accept_trade_offer,
    # офферы по цене на лоты маркета:
    create_listing_offer, get_listing_offer, get_user_listing_offers,
    decline_listing_offer, cancel_listing_offer, accept_listing_offer,
    # завершённые обмены для истории сделок:
    get_user_completed_trades, get_user_deal_count,
)

# Комиссия (GRAM) за вывод нативного TG-подарка — окупает 25 Stars трансфера
GIFT_WITHDRAW_FEE: float = float(os.getenv("GIFT_WITHDRAW_FEE_TON", "0.25"))

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Бизнес-константы. Берём из config, если он импортируется на Railway,
# иначе — из env/значений по умолчанию (реф-бонус по умолчанию 0, чтобы не начислить лишнего).
try:
    from config import MARKET_FEE, REFERRAL_BONUS_PERCENT
except Exception:
    MARKET_FEE = float(os.getenv("MARKET_FEE", "0.03"))
    REFERRAL_BONUS_PERCENT = float(os.getenv("REFERRAL_BONUS_PERCENT", "0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(app: FastAPI):
    from escrow_wallet import get_escrow_wallet
    get_escrow_wallet()
    await init_db()
    poller = asyncio.create_task(deposits.poll_loop())
    yield
    poller.cancel()
    await close_pool()

app = FastAPI(title="GiftSafe API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_telegram_data(init_data: str) -> Optional[dict]:
    """Верифицируем initData от Telegram WebApp.

    Важно: значения в initData URL-кодированы, поэтому парсим через parse_qsl
    (он декодирует), иначе hash не сойдётся и user не распарсится.
    """
    if not init_data or not BOT_TOKEN:
        return None
    try:
        parsed = dict(parse_qsl(init_data))
        hash_val = parsed.pop("hash", "")
        if not hash_val:
            return None
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        check = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(check, hash_val):
            return None
        return json.loads(parsed.get("user", "{}"))
    except Exception:
        return None


def get_user_from_header(x_telegram_init_data: str = "") -> Optional[dict]:
    if not x_telegram_init_data:
        return None
    return verify_telegram_data(x_telegram_init_data)


# ── Парсинг Telegram-подарка ───────────────────────────────────────────────────

TELEGRAM_GIFT_RE = re.compile(r"t\.me/nft/([^/?#\s]+)", re.IGNORECASE)


def parse_gift_url(url: str) -> Optional[dict]:
    """Из ссылки вида https://t.me/nft/SakuraFlower-33824 достаём
    коллекцию ("Sakura Flower"), номер ("#33824") и слаг."""
    m = TELEGRAM_GIFT_RE.search((url or "").strip())
    if not m:
        return None
    slug = m.group(1)  # напр. "SakuraFlower-33824"
    if "-" in slug:
        name_part, num = slug.rsplit("-", 1)
    else:
        name_part, num = slug, ""
    # CamelCase -> "Camel Case"
    collection = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name_part).strip() or name_part
    number = f"#{num}" if num.isdigit() else ""
    return {"slug": slug, "collection": collection, "number": number}


def gift_slug_from(name: str, number) -> str:
    """Собирает слаг t.me/nft из имени коллекции и номера.
    "Sakura Flower" + "33824"/"#33824" -> "SakuraFlower-33824".
    Нужен для подарков из Rubuy Bank, у которых nft_address пуст."""
    num = re.sub(r"[#\s]", "", str(number or ""))
    nm = re.sub(r"\s+", "", str(name or ""))
    return f"{nm}-{num}" if nm and num else ""


def fetch_gift_meta(url: str) -> dict:
    """Best-effort: тянем og:title и og:image со страницы подарка.
    Никогда не роняет создание лота — при любой ошибке возвращаем {}."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "ignore")

        def og(prop: str) -> str:
            mm = re.search(
                r'<meta[^>]+property=["\']og:' + prop + r'["\'][^>]+content=["\']([^"\']+)["\']',
                html, re.IGNORECASE,
            )
            return mm.group(1) if mm else ""

        return {"title": og("title"), "image": og("image")}
    except Exception:
        return {}


async def notify_seller(seller_id: int, text: str):
    """Best-effort уведомление продавцу в Telegram. Не роняет покупку при сбое.

    Используем urllib из стандартной библиотеки (без новых зависимостей),
    отправку выносим в поток, чтобы не блокировать event loop.
    """
    if not BOT_TOKEN:
        return

    def _send():
        data = json.dumps({
            "chat_id": seller_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10).read()

    try:
        await asyncio.to_thread(_send)
    except Exception:
        pass


# ===== LISTINGS =====

@app.get("/api/listings")
async def listings(
    limit: int = Query(20, le=50),
    offset: int = 0,
    collection: Optional[str] = None,
    max_price: Optional[float] = None,
    x_telegram_init_data: Optional[str] = Header(None),
):
    items = await get_active_listings(limit=limit, offset=offset,
                                       collection=collection, max_price=max_price)
    return {"listings": items, "total": len(items)}

@app.get("/api/listings/{listing_id}")
async def listing_detail(listing_id: int):
    item = await get_listing(listing_id)
    if not item:
        raise HTTPException(404, "Лот не найден")
    return item


class CreateListingBody(BaseModel):
    gift_id: Optional[int] = None   # задепозиченный NFT (новый путь)
    gift_url: Optional[str] = None  # ссылка t.me/nft/... (старый путь)
    price: float
    description: str = ""


@app.post("/api/listings")
async def create_listing_endpoint(
    body: CreateListingBody,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")

    if body.price <= 0:
        raise HTTPException(400, "Цена должна быть больше нуля")

    # Новый путь: подарок уже задепозичен, создаём листинг по gift_id
    if body.gift_id:
        gift = await get_gift(body.gift_id)
        if not gift or gift["owner_id"] != user["id"]:
            raise HTTPException(404, "Подарок не найден или принадлежит не вам")
        if await gift_is_locked(body.gift_id):
            raise HTTPException(409, "Этот подарок уже занят (продажа/аукцион/обмен)")
        try:
            listing_id = await create_listing(
                gift_id=body.gift_id,
                seller_id=user["id"],
                price_ton=body.price,
                description=body.description,
            )
        except Exception as e:
            # гонка двух параллельных запросов упрётся в уникальный индекс
            if "uq_listings_active_gift" in str(e):
                raise HTTPException(409, "Этот подарок уже выставлен на продажу")
            raise
        return {
            "ok": True,
            "listing_id": listing_id,
            "gift_id": body.gift_id,
            "gift_name": gift["gift_name"],
        }

    if not body.gift_url:
        raise HTTPException(400, "Укажите подарок для продажи")

    parsed = parse_gift_url(body.gift_url)

    # Продавец мог открыть Mini App, не нажимая /start в боте —
    # гарантируем, что он есть в users (иначе FK на owner_id упадёт).
    full_name = " ".join(
        p for p in [user.get("first_name"), user.get("last_name")] if p
    )
    await get_or_create_user(user["id"], user.get("username", ""), full_name)

    # Метаданные подарка (картинка/имя) — best-effort, не критично для лота.
    meta = await asyncio.to_thread(fetch_gift_meta, body.gift_url)
    raw_title = (meta.get("title") or "").strip()
    if raw_title:
        # убираем возможный хвост "#33824", чтобы не дублировать номер в карточке
        gift_name = re.sub(r"\s*#?\d+\s*$", "", raw_title).strip() or raw_title
    else:
        gift_name = parsed["collection"] or parsed["slug"]
    image_url = meta.get("image", "")

    # Создаём запись о подарке во владении продавца…
    gift_id = await add_gift(
        owner_id=user["id"],
        collection_name=parsed["collection"],
        gift_name=gift_name,
        gift_number=parsed["number"],
        rarity="Common",           # реальную редкость Telegram-ссылка не отдаёт
        image_url=image_url,
        nft_address=parsed["slug"],
    )

    # …и уже потом сам листинг в TON.
    listing_id = await create_listing(
        gift_id=gift_id,
        seller_id=user["id"],
        price_ton=body.price,
        description=body.description,
    )
    return {"ok": True, "listing_id": listing_id, "gift_id": gift_id, "gift_name": gift_name}


@app.post("/api/listings/{listing_id}/buy")
async def buy_listing(
    listing_id: int,
    x_telegram_init_data: Optional[str] = Header(None),
):
    tg_user = get_user_from_header(x_telegram_init_data or "")
    if not tg_user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")

    lst = await get_listing(listing_id)
    if not lst or lst["status"] != "active":
        raise HTTPException(400, "Лот недоступен — уже продан или снят с продажи")

    buyer_id = tg_user["id"]
    seller_id = lst["seller_id"]
    if buyer_id == seller_id:
        raise HTTPException(400, "Нельзя купить собственный лот")

    # Гарантируем, что покупатель есть в БД (мог открыть Mini App, не нажимая /start в боте)
    full_name = " ".join(
        p for p in [tg_user.get("first_name"), tg_user.get("last_name")] if p
    )
    buyer = await get_or_create_user(buyer_id, tg_user.get("username", ""), full_name)

    price = lst["price_ton"]
    fee = price * MARKET_FEE

    seller = await get_user(seller_id)
    ref_bonus = 0.0
    # Гард от самореферала: покупатель-рефер продавца не получает бонус
    # со своей же покупки (иначе это скрытая скидка на лоты своих рефералов).
    if (
            seller
            and seller.get("referred_by")
            and seller["referred_by"] != buyer_id
    ):
        ref_bonus = price * REFERRAL_BONUS_PERCENT

    # Демо-режим: списываем с внутреннего баланса. В проде — реальный TON-платёж.
    if buyer["balance_ton"] < price - 1e-6:
        raise HTTPException(
            400, f"Недостаточно средств: на балансе {buyer['balance_ton']:.4f} Gram, нужно {price:.4f} Gram"
        )

    # Движение средств. Реф-бонус платится ИЗ комиссии площадки (наша доля
    # становится fee − ref_bonus), продавец получает ровно обещанные price − fee.
    await update_balance(buyer_id, -price)
    seller_net = price - fee
    await update_balance(seller_id, seller_net)

    # Перевод гифта покупателю
    await transfer_gift(lst["gift_id"], buyer_id)

    # Помечаем лот проданным
    await mark_listing_sold(listing_id)

    # Запись транзакции (внутри обновляет total_spent / total_earned)
    tx_id = await record_transaction(
        buyer_id, seller_id, lst["gift_id"],
        price, fee, ref_bonus, "listing", listing_id
    )

    # Реферальная выплата
    if ref_bonus > 0 and seller.get("referred_by"):
        await update_balance(seller["referred_by"], ref_bonus)
        await record_referral_payout(seller["referred_by"], seller_id, tx_id, ref_bonus)

    # Ссылка на сам подарок (t.me/nft/<slug>). Для подарков из Rubuy Bank
    # nft_address пуст — собираем слаг из имени и номера.
    gift_slug = (lst.get("nft_address") or "").strip() or gift_slug_from(
        lst.get("gift_name"), lst.get("gift_number")
    )
    gift_link = f"https://t.me/nft/{gift_slug}" if gift_slug else ""
    # Короткий видимый текст ссылки (t.me/nft/…) — чтобы строка не переносилась
    # по ширине пузыря; href полный, карточку-превью Telegram берёт из него.
    link_line = f'\n🔗 <a href="{gift_link}">t.me/nft/{gift_slug}</a>' if gift_link else ""

    # Уведомление продавцу (best-effort)
    buyer_name = tg_user.get("username") or tg_user.get("first_name") or "покупатель"
    await notify_seller(
        seller_id,
        f"🎉 <b>Ваш подарок продан!</b>\n\n"
        f"🎁 {lst['gift_name']} #{lst.get('gift_number','?')}\n"
        f"💰 Вы получили: {seller_net:.4f} TON\n"
        f"👤 Покупатель: @{buyer_name}"
        + link_line
    )

    # Уведомление покупателю со ссылкой на подарок (best-effort)
    await notify_seller(
        buyer_id,
        f"✅ <b>Покупка совершена!</b>\n\n"
        f"🎁 {lst['gift_name']} #{lst.get('gift_number','?')}\n"
        f"💰 Списано: {price:.4f} TON"
        + link_line
    )

    return {
        "ok": True,
        "gift_name": lst["gift_name"],
        "gift_number": lst.get("gift_number", ""),
        "price": price,
        "fee": fee,
        "seller_net": seller_net,
    }


# ===== ОФФЕРЫ ПО ЦЕНЕ (ЛОТЫ МАРКЕТА) =====

MIN_OFFER_FRACTION = 0.5  # оффер не может быть меньше 50% цены лота


class ListingOfferBody(BaseModel):
    amount_ton: float


@app.post("/api/listings/{listing_id}/offer")
async def create_listing_offer_endpoint(
    listing_id: int,
    body: ListingOfferBody,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")

    lst = await get_listing(listing_id)
    if not lst or lst["status"] != "active":
        raise HTTPException(404, "Лот не найден")
    if lst["seller_id"] == user["id"]:
        raise HTTPException(400, "Нельзя предложить цену на собственный лот")

    min_amount = lst["price_ton"] * MIN_OFFER_FRACTION
    if body.amount_ton < min_amount:
        raise HTTPException(400, f"Оффер не может быть меньше {min_amount:.2f} Gram (50% цены)")

    full_name = " ".join(p for p in [user.get("first_name"), user.get("last_name")] if p)
    db_user = await get_or_create_user(user["id"], user.get("username", ""), full_name)
    if db_user["balance_ton"] < body.amount_ton:
        raise HTTPException(
            400, f"Недостаточно средств: баланс {db_user['balance_ton']:.2f} Gram, для оффера требуется {body.amount_ton:.2f} Gram"
        )

    offer_id = await create_listing_offer(listing_id, user["id"], body.amount_ton)

    from_name = user.get("username") or full_name or "пользователь"
    await notify_seller(
        lst["seller_id"],
        f"💬 <b>Вам предложили цену!</b>\n\n"
        f"🎁 {lst['gift_name']} #{lst.get('gift_number','?')}\n"
        f"🏷 Цена лота: {lst['price_ton']:.2f} Gram\n"
        f"💰 Предложено: {body.amount_ton:.2f} Gram\n"
        f"👤 От: @{from_name}\n\nСмотри в Профиле → Офферы."
    )
    return {"ok": True, "offer_id": offer_id}


@app.get("/api/listings/offers/mine")
async def my_listing_offers(x_telegram_init_data: Optional[str] = Header(None)):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    return await get_user_listing_offers(user["id"])


@app.post("/api/listings/offers/{offer_id}/accept")
async def accept_listing_offer_endpoint(
    offer_id: int,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    offer = await get_listing_offer(offer_id)
    if not offer or offer["seller_id"] != user["id"]:
        raise HTTPException(404, "Предложение не найдено")
    if offer["status"] != "pending":
        raise HTTPException(409, "Предложение уже не активно")

    error, result = await accept_listing_offer(offer_id, MARKET_FEE, REFERRAL_BONUS_PERCENT)
    if error:
        raise HTTPException(409, error)

    gift = await get_gift(result["gift_id"])
    await notify_seller(
        result["buyer_id"],
        f"🎉 <b>Ваше предложение цены принято!</b>\n\n"
        f"🎁 {gift['gift_name']} #{gift.get('gift_number','?')}\n"
        f"💰 Списано: {result['price']:.4f} Gram\n\nСмотри в Портфеле."
    )
    return {"ok": True}


@app.post("/api/listings/offers/{offer_id}/decline")
async def decline_listing_offer_endpoint(
    offer_id: int,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    offer = await get_listing_offer(offer_id)
    if not offer or offer["seller_id"] != user["id"]:
        raise HTTPException(404, "Предложение не найдено")
    await decline_listing_offer(offer_id)
    return {"ok": True}


@app.post("/api/listings/offers/{offer_id}/cancel")
async def cancel_listing_offer_endpoint(
    offer_id: int,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    ok = await cancel_listing_offer(offer_id, user["id"])
    if not ok:
        raise HTTPException(404, "Предложение не найдено или принадлежит не вам")
    return {"ok": True}


# ===== AUCTIONS =====

@app.get("/api/auctions")
async def auctions(limit: int = 20, offset: int = 0):
    items = await get_active_auctions(limit=limit, offset=offset)
    return {"auctions": items}


@app.get("/api/auctions/{auction_id}")
async def auction_detail(auction_id: int):
    item = await get_auction(auction_id)
    if not item:
        raise HTTPException(404, "Аукцион не найден")
    return item


class BidBody(BaseModel):
    amount: float


@app.post("/api/auctions/{auction_id}/bid")
async def bid(
    auction_id: int,
    body: BidBody,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    ok = await place_bid(auction_id=auction_id, bidder_id=user["id"], amount=body.amount)
    if not ok:
        raise HTTPException(400, "Ставка слишком мала или аукцион завершён")
    return {"ok": True}


# ===== ОБМЕН (TRADES) =====

@app.get("/api/trades")
async def trades(limit: int = Query(20, le=50), offset: int = 0):
    items = await get_active_trade_listings(limit=limit, offset=offset)
    return {"trades": items}


@app.get("/api/trades/{trade_id}")
async def trade_detail(trade_id: int):
    item = await get_trade_listing(trade_id)
    if not item:
        raise HTTPException(404, "Объявление об обмене не найдено")
    return item


class CreateTradeBody(BaseModel):
    gift_ids: List[int]
    note: str = ""


def _gift_list_summary(gifts: list) -> str:
    """'Tama Gadget #123, Vice Cream #456' — для уведомлений."""
    return ", ".join(
        f"{g['gift_name']}{' #' + g['gift_number'] if g.get('gift_number') else ''}"
        for g in gifts
    )


@app.post("/api/trades")
async def create_trade_endpoint(
    body: CreateTradeBody,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    gift_ids = list(dict.fromkeys(body.gift_ids))  # без дублей, порядок сохраняем
    if not gift_ids:
        raise HTTPException(400, "Выберите хотя бы один подарок")
    for gift_id in gift_ids:
        gift = await get_gift(gift_id)
        if not gift or gift["owner_id"] != user["id"]:
            raise HTTPException(404, "Подарок не найден или принадлежит не вам")
        if await gift_is_locked(gift_id):
            raise HTTPException(409, "Этот подарок уже занят (продажа/аукцион/обмен)")
    trade_id = await create_trade_listing(gift_ids, user["id"], body.note)
    return {"ok": True, "trade_id": trade_id}


@app.delete("/api/trades/{trade_id}")
async def cancel_trade_endpoint(
    trade_id: int,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    trade = await get_trade_listing(trade_id)
    if not trade or trade["owner_id"] != user["id"]:
        raise HTTPException(404, "Объявление об обмене не найдено")
    await cancel_trade_listing(trade_id)
    return {"ok": True}


class TradeOfferBody(BaseModel):
    offered_gift_ids: List[int]
    top_up_ton: float = 0.0


@app.post("/api/trades/{trade_id}/offer")
async def create_trade_offer_endpoint(
    trade_id: int,
    body: TradeOfferBody,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    if body.top_up_ton < 0:
        raise HTTPException(400, "Доплата не может быть отрицательной")
    if body.top_up_ton > 0:
        db_user = await get_user(user["id"])
        balance = db_user["balance_ton"] if db_user else 0.0
        # Небольшой допуск на погрешность float — иначе баланс, который на экране
        # округлён до "0.58", но хранится как 0.5799999999999998, ложно считался
        # бы недостаточным для доплаты ровно в 0.58.
        if balance < body.top_up_ton - 1e-6:
            raise HTTPException(
                400,
                f"Недостаточно баланса для доплаты: на балансе {balance:.2f} Gram, "
                f"для доплаты требуется {body.top_up_ton:.2f} Gram"
            )

    trade = await get_trade_listing(trade_id)
    if not trade or trade["status"] != "active":
        raise HTTPException(404, "Объявление об обмене не найдено")
    if trade["owner_id"] == user["id"]:
        raise HTTPException(400, "Нельзя предложить обмен на собственное объявление")

    gift_ids = list(dict.fromkeys(body.offered_gift_ids))
    if not gift_ids:
        raise HTTPException(400, "Выберите хотя бы один подарок")
    offered_gifts = []
    for gift_id in gift_ids:
        gift = await get_gift(gift_id)
        if not gift or gift["owner_id"] != user["id"]:
            raise HTTPException(404, "Предлагаемый подарок не найден или принадлежит не вам")
        if await gift_is_locked(gift_id):
            raise HTTPException(409, "Этот подарок уже занят (продажа/аукцион/обмен)")
        offered_gifts.append(gift)

    offer_id = await create_trade_offer(trade_id, user["id"], gift_ids, body.top_up_ton)

    full_name = " ".join(p for p in [user.get("first_name"), user.get("last_name")] if p)
    from_name = user.get("username") or full_name or "пользователь"
    await notify_seller(
        trade["owner_id"],
        f"🔄 <b>Вам предложили обмен!</b>\n\n"
        f"🎁 За: {_gift_list_summary(trade['gifts'])}\n"
        f"🎁 Предлагают: {_gift_list_summary(offered_gifts)}"
        + (f"\n💎 + доплата {body.top_up_ton:.2f} Gram" if body.top_up_ton > 0 else "")
        + f"\n👤 От: @{from_name}\n\nСмотри в Профиле → Офферы."
    )
    return {"ok": True, "offer_id": offer_id}


@app.get("/api/trades/offers/mine")
async def my_trade_offers(x_telegram_init_data: Optional[str] = Header(None)):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    return await get_user_trade_offers(user["id"])


@app.post("/api/trades/offers/{offer_id}/accept")
async def accept_trade_offer_endpoint(
    offer_id: int,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    offer = await get_trade_offer(offer_id)
    if not offer or offer["to_user_id"] != user["id"]:
        raise HTTPException(404, "Предложение не найдено")
    if offer["status"] != "pending":
        raise HTTPException(409, "Предложение уже не активно")

    error = await accept_trade_offer(offer_id, MARKET_FEE)
    if error:
        raise HTTPException(409, error)

    await notify_seller(
        offer["from_user_id"],
        "🎉 <b>Ваше предложение обмена принято!</b>\n\nСмотри в Портфеле — подарок уже у вас.",
    )
    return {"ok": True}


@app.post("/api/trades/offers/{offer_id}/decline")
async def decline_trade_offer_endpoint(
    offer_id: int,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    offer = await get_trade_offer(offer_id)
    if not offer or offer["to_user_id"] != user["id"]:
        raise HTTPException(404, "Предложение не найдено")
    await decline_trade_offer(offer_id)
    return {"ok": True}


@app.post("/api/trades/offers/{offer_id}/cancel")
async def cancel_trade_offer_endpoint(
    offer_id: int,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    ok = await cancel_trade_offer(offer_id, user["id"])
    if not ok:
        raise HTTPException(404, "Предложение не найдено или принадлежит не вам")
    return {"ok": True}


# ===== PORTFOLIO =====

@app.get("/api/portfolio")
async def portfolio(x_telegram_init_data: Optional[str] = Header(None)):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    gifts = await get_user_gifts(user["id"])
    result = []
    for g in gifts:
        g = dict(g)
        active_listing = await get_active_listing_for_gift(g["gift_id"])
        g["on_sale"] = bool(active_listing)
        g["listing_id"] = active_listing["listing_id"] if active_listing else None
        g["price_ton"] = active_listing["price_ton"] if active_listing else None
        active_trade = await get_active_trade_listing_for_gift(g["gift_id"])
        g["on_trade"] = bool(active_trade)
        g["trade_id"] = active_trade["trade_id"] if active_trade else None
        result.append(g)
    return {"gifts": result}


# ===== PROFILE =====

@app.get("/api/profile")
async def profile(x_telegram_init_data: Optional[str] = Header(None)):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    db_user = await get_user(user["id"])
    txs = await get_user_transactions(user["id"], limit=25)
    trades = await get_user_completed_trades(user["id"], limit=25)
    total_deals = await get_user_deal_count(user["id"])
    for tx in txs:
        tx["kind"] = "sale"
    for tr in trades:
        tr["kind"] = "trade"
    history = sorted(txs + trades, key=lambda x: x["completed_at"], reverse=True)[:25]
    trade_offers = await get_user_trade_offers(user["id"])
    listing_offers = await get_user_listing_offers(user["id"])
    pending = len(trade_offers["incoming"]) + len(listing_offers["incoming"])
    return {
        "user": db_user, "transactions": history, "pending_offers": pending,
        "total_deals": total_deals,
    }


# ===== STATS =====

@app.get("/api/stats")
async def stats():
    return await get_platform_stats()

@app.get("/api/escrow/status")
async def escrow_status():
    return await ton_client.get_escrow_snapshot()


@app.post("/api/escrow/deposit-intent")
async def create_deposit_intent_endpoint(
        x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    if not ton_client.is_configured():
        raise HTTPException(503, "Сервис временно недоступен — кошелёк-сейф не настроен")

    full_name = " ".join(
        p for p in [user.get("first_name"), user.get("last_name")] if p
    )
    await get_or_create_user(user["id"], user.get("username", ""), full_name)

    intent = await get_or_create_deposit_intent(user["id"])
    return {
        "ok": True,
        "address": ton_client.TON_WALLET_ADDRESS,
        "code": intent["code"],
        "network": ton_client.TON_NETWORK,
        "instructions": "Отправьте NFT на этот адрес, указав код в комментарии к переводу.",
    }

ADDR_RE = re.compile(r"^(-?\d+:[0-9a-fA-F]{64}|[A-Za-z0-9_-]{48})$")

class WithdrawBody(BaseModel):
    to_address: Optional[str] = None  # нужен только для ончейн-NFT

@app.post("/api/gifts/{gift_id}/withdraw")
async def withdraw_gift(
        gift_id: int,
        body: WithdrawBody,
        x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")

    gift = await get_gift(gift_id)
    if not gift or gift.get("owner_id") != user["id"]:
        raise HTTPException(404, "Подарок не найден")

    locks = await get_gift_locks(gift_id)
    if any(locks.values()):
        in_trade = locks["trade_listings"] or locks["trade_offers"]
        raise HTTPException(409, "Подарок находится в обмене" if in_trade
                            else "Подарок находится на продаже")

    tg_owned_gift_id = gift.get("tg_owned_gift_id") or ""
    if tg_owned_gift_id:
        # ── Нативный Telegram-подарок: возврат на аккаунт владельца, без адреса ──
        import tg_gifts
        if not await tg_gifts.is_configured():
            raise HTTPException(503, "Rubuy Bank временно недоступен, попробуйте позже")

        # Комиссия за передачу: покрывает Stars, которые Telegram списывает
        # с бизнес-аккаунта за transferGift. Списываем до отправки, при фейле
        # возвращаем вместе с владением.
        fee = GIFT_WITHDRAW_FEE
        if fee > 0 and not await try_charge_balance(user["id"], fee):
            raise HTTPException(
                402,
                f"Для вывода нужно {fee:g} Gram на балансе — "
                f"комиссия за передачу подарка",
            )

        await set_gift_owner(gift_id, None)
        try:
            await tg_gifts.transfer_unique_gift(tg_owned_gift_id, user["id"])
        except Exception as e:
            await set_gift_owner(gift_id, user["id"])
            if fee > 0:
                await update_balance(user["id"], fee)
            logger.warning("TG gift withdraw failed for gift %s: %s", gift_id, e)
            raise HTTPException(
                502,
                "Не удалось вернуть подарок в Telegram. Попробуйте позже — "
                "гифт остался в вашем портфеле, комиссия возвращена",
            )
        logger.info("🎁➡️ Вывод гифта %s → user %s (комиссия %.2f)",
                    gift_id, user["id"], fee)
        return {"ok": True, "delivered_to": "telegram", "fee": fee}

    nft_address = gift.get("nft_address") or ""
    if not nft_address:
        raise HTTPException(409, "У этого подарка нет NFT в блокчейне TON")

    to_address = (body.to_address or "").strip()
    if not ADDR_RE.match(to_address):
        raise HTTPException(400, "Неверный адрес TON-кошелька")
    if to_address == ton_client.TON_WALLET_ADDRESS:
        raise HTTPException(400, "Нельзя выводить на кошелёк-сейф сервиса")

    # Снимаем владение ДО отправки — чтобы гифт нельзя было выставить,
    # пока транзакция в полёте. При фейле откатываем.
    await set_gift_owner(gift_id, None)
    try:
        from escrow_wallet import send_nft
        tx = await send_nft(nft_address, to_address,
                            comment="GiftSafe: NFT delivery")
    except Exception as e:
        await set_gift_owner(gift_id, user["id"])
        logger.warning("Gift withdraw failed for gift %s: %s", gift_id, e)
        raise HTTPException(502, "Не удалось отправить NFT — подарок возвращён в портфель")

    return {"ok": True, "tx": tx, "sent_to": to_address}

MIN_WITHDRAW_TON = 0.1

class BalanceWithdrawBody(BaseModel):
    to_address: str
    amount: float

@app.post("/api/balance/withdraw")
async def withdraw_balance(
        body: BalanceWithdrawBody,
        x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")

    amount = round(body.amount, 2)
    if amount < MIN_WITHDRAW_TON:
        raise HTTPException(400, f"Минимум для вывода — {MIN_WITHDRAW_TON} TON")

    to_address = body.to_address.strip()
    if not ADDR_RE.match(to_address):
        raise HTTPException(400, "Неверный адрес TON-кошелька")
    if to_address == ton_client.TON_WALLET_ADDRESS:
        raise HTTPException(400, "Нельзя выводить на кошелёк-сейф сервиса")

    # Гард: на сейфе должно хватать TON (+ запас на сетевую комиссию), иначе
    # ончейн-отправка провалится и юзер зря прождёт 15-минутный рефанд C-4.
    try:
        escrow_balance = await ton_client.get_wallet_balance()
    except Exception:
        escrow_balance = None  # TON API моргнул — не блокируем, C-4 подстрахует
    if escrow_balance is not None and escrow_balance < amount + 0.05:
        logger.warning("Вывод отклонён: на сейфе %.2f TON, запрошено %.2f (user %s)",
                       escrow_balance, amount, user["id"])
        raise HTTPException(
            503,
            "Вывод временно недоступен — недостаточно средств на кошельке сервиса. "
            "Попробуйте меньшую сумму или зайдите позже",
        )

    # C-4: списание баланса и создание записи вывода — атомарно, под локом
    # строки users (закрывает и гонку двух одновременных выводов).
    wd_id = await create_withdrawal(user["id"], to_address, amount)
    if not wd_id:
        raise HTTPException(409, "Недостаточно средств")

    try:
        from escrow_wallet import send_ton
        # Уникальный комментарий — по нему поллер подтвердит вывод в блокчейне.
        tx = await send_ton(to_address, amount,
                            comment=f"GiftSafe: withdrawal #{wd_id}")
        await mark_withdrawal_sent(wd_id, tx)
    except Exception as e:
        # НЕ возвращаем баланс сразу: исключение не гарантирует, что TON не ушли
        # (нода могла принять сообщение, а ответ — потеряться). Решает поллер:
        # найдёт транзакцию → confirmed; не найдёт за грейс-период → refunded.
        logger.warning("TON withdraw #%s send error for user %s: %s",
                       wd_id, user["id"], e)
        raise HTTPException(
            502,
            "Отправка не подтверждена. Если TON не придут в течение ~15 минут, "
            "баланс вернётся автоматически",
        )

    return {"ok": True, "tx": tx, "amount": amount, "sent_to": to_address, "wd_id": wd_id}

class PriceBody(BaseModel):
    price: float


@app.post("/api/listings/{listing_id}/price")
async def change_listing_price(
        listing_id: int,
        body: PriceBody,
        x_telegram_init_data: Optional[str] = Header(None),
):
    """Смена цены своего активного лота."""
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    if body.price <= 0:
        raise HTTPException(400, "Цена должна быть больше нуля")

    listing = await get_listing(listing_id)
    if not listing:
        raise HTTPException(404, "Лот не найден")
    if listing["seller_id"] != user["id"]:
        raise HTTPException(403, "Это не ваш лот")
    if listing["status"] != "active":
        raise HTTPException(409, "Лот уже не активен")

    price = round(body.price, 4)
    if not await set_listing_price(listing_id, price):
        raise HTTPException(409, "Лот уже не активен")
    return {"ok": True, "price": price}


@app.post("/api/escrow/withdraw/{listing_id}")
async def withdraw_listing(
        listing_id: int,
        x_telegram_init_data: Optional[str] = Header(None),
):
    """Снятие лота с продажи (MRKT-модель).

    Только гасим лот: NFT остаётся в сейфе, подарок — в портфеле продавца.
    Ончейн-возврат на кошелёк — отдельная ручка /api/gifts/{id}/withdraw."""
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")

    listing = await get_listing(listing_id)
    if not listing:
        raise HTTPException(404, "Лот не найден")
    if listing["seller_id"] != user["id"]:
        raise HTTPException(403, "Это не ваш лот")
    if listing["status"] != "active":
        raise HTTPException(409, "Лот уже не активен")

    await set_listing_status(listing_id, "cancelled")
    return {"ok": True, "delisted": True, "gift_id": listing["gift_id"]}

@app.get("/api/escrow/deposit-intent")
async def deposit_intent_status(
        x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    intent = await get_latest_intent_for_user(user["id"])
    if not intent:
        return {"status": "none"}
    return {
        "status": intent["status"],  # pending / completed
        "code": intent["code"],
        "gift_id": intent["gift_id"],
        "nft_address": intent["nft_address"],
    }

@app.get("/api/referral/stats")
async def referral_stats(
        x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")
    return await get_referral_stats(user["id"])


# ===== TELEGRAM FILE PROXY =====
# Стикеры подарков нельзя отдавать фронту прямой ссылкой — в ней токен бота.
# Прокси: file_id → getFile → стрим содержимого. file_path у Telegram живёт
# ~час, поэтому резолвим на каждый запрос, а браузеру разрешаем кешировать.

FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,200}$")

@app.get("/api/tg-file/{file_id}")
async def tg_file_proxy(file_id: str):
    if not BOT_TOKEN or not FILE_ID_RE.match(file_id):
        raise HTTPException(404, "Не найдено")

    def _fetch() -> tuple[bytes, str]:
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
            data=json.dumps({"file_id": file_id}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        info = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if not info.get("ok"):
            raise ValueError(info.get("description", "getFile failed"))
        file_path = info["result"]["file_path"]
        blob = urllib.request.urlopen(
            f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}", timeout=15
        ).read()
        return blob, file_path

    try:
        blob, file_path = await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.warning("tg-file proxy %s: %s", file_id[:16], e)
        raise HTTPException(404, "Файл недоступен")

    ext = file_path.rsplit(".", 1)[-1].lower()
    media_type = {
        "tgs": "application/gzip",   # gzip-нутый Lottie JSON
        "webp": "image/webp",
        "webm": "video/webm",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
    }.get(ext, "application/octet-stream")

    from fastapi import Response
    return Response(
        content=blob,
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
            # Всегда, а не только при Origin в запросе: файл кешируется в
            # браузере на сутки, и если первый запрос был без CORS (обычный
            # <img>), закешированный ответ без этого заголовка ломает
            # последующие CORS-загрузки того же URL (fetch, CSS mask-image).
            "Access-Control-Allow-Origin": "*",
        },
    )


# ===== ADMIN =====
# Прямой доступ к Railway-постгресу с локальной машины порезан провайдером,
# поэтому ручные операции (привязка unclaimed-подарков и т.п.) — через API.
# Авторизация: X-Admin-Token == BOT_TOKEN (владелец токена и так управляет ботом).

def _check_admin(token: Optional[str]) -> None:
    if not BOT_TOKEN or token != BOT_TOKEN:
        raise HTTPException(401, "Требуется авторизация — откройте приложение в Telegram")


@app.get("/api/admin/overview")
async def admin_overview(x_admin_token: Optional[str] = Header(None)):
    """Юзеры и подарки одним экраном — замена ручных SELECT в дашборде."""
    _check_admin(x_admin_token)
    pool = await get_pool()
    users = [dict(r) for r in await pool.fetch(
        "SELECT user_id, username, full_name, balance_ton, referred_by FROM users ORDER BY user_id")]
    gifts = [dict(r) for r in await pool.fetch(
        """SELECT gift_id, owner_id, gift_name, gift_number, rarity, nft_address,
                  tg_owned_gift_id, tg_sticker, tg_thumb
           FROM gifts ORDER BY gift_id""")]
    conns = [dict(r) for r in await pool.fetch(
        "SELECT * FROM business_connections ORDER BY updated_at DESC")]
    return {"users": users, "gifts": gifts, "business_connections": conns}


class ReassignBody(BaseModel):
    user_id: Optional[int] = None  # None → снять владельца (unclaimed), без удаления строки


@app.post("/api/admin/gifts/{gift_id}/reassign")
async def admin_reassign_gift(
        gift_id: int,
        body: ReassignBody,
        x_admin_token: Optional[str] = Header(None),
):
    """Ручная привязка подарка (unclaimed / ошибочная атрибуция) к юзеру, либо
    (user_id=None) снятие владельца — напр. чтобы убрать дубликат-гифт с
    портфеля тестового аккаунта, не удаляя саму строку (сохраняет ссылки из
    transactions на реальные прошлые продажи по этому gift_id нетронутыми)."""
    _check_admin(x_admin_token)
    gift = await get_gift(gift_id)
    if not gift:
        raise HTTPException(404, "Подарок не найден")
    if await gift_is_locked(gift_id):
        raise HTTPException(409, "Подарок на продаже — сначала снимите лот")
    if body.user_id is not None:
        await get_or_create_user(body.user_id, "", "")
    await set_gift_owner(gift_id, body.user_id)
    logger.info("👮 Admin: gift %s переприсвоен user %s (был %s)",
                gift_id, body.user_id, gift.get("owner_id"))
    return {"ok": True, "gift_id": gift_id,
            "old_owner": gift.get("owner_id"), "new_owner": body.user_id}


@app.post("/api/admin/listings/{listing_id}/cancel")
async def admin_cancel_listing(
        listing_id: int,
        x_admin_token: Optional[str] = Header(None),
):
    """Ручная отмена зависшего лота (напр. созданного на подарок, который потом
    ушёл владельцу через Обмен, оставив дублирующийся активный листинг)."""
    _check_admin(x_admin_token)
    await cancel_listing(listing_id)
    return {"ok": True}


@app.get("/api/admin/gifts/{gift_id}/locks")
async def admin_gift_locks(
        gift_id: int,
        x_admin_token: Optional[str] = Header(None),
):
    """Диагностика: чем именно занят гифт (лот/аукцион/обмен/оффер), раз прямой
    доступ к Postgres с локальной машины порезан провайдером."""
    _check_admin(x_admin_token)
    return await get_gift_locks(gift_id)


@app.delete("/api/admin/gifts/{gift_id}")
async def admin_delete_gift(
        gift_id: int,
        x_admin_token: Optional[str] = Header(None),
):
    """Ручное удаление ошибочной/дубликат-строки гифта (напр. задвоенный
    TG-гифт, заведённый до фикса реконсиляции ре-депозитов в process_tg_gifts)."""
    _check_admin(x_admin_token)
    gift = await get_gift(gift_id)
    if not gift:
        raise HTTPException(404, "Подарок не найден")
    if await gift_is_locked(gift_id):
        raise HTTPException(409, "Подарок на продаже или в обмене — сначала снимите")
    await delete_gift(gift_id)
    logger.info("👮 Admin: gift %s удалён (был owner=%s, %s #%s)",
                gift_id, gift.get("owner_id"), gift.get("gift_name"), gift.get("gift_number"))
    return {"ok": True, "deleted_gift_id": gift_id}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)