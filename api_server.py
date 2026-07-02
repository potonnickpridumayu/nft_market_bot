"""FastAPI сервер для GiftSafe Mini App"""
import hmac
import hashlib
import json
import os
import re
import asyncio
import ton_client
import deposits
import urllib.request
from typing import Optional
from urllib.parse import parse_qsl

from contextlib import asynccontextmanager
from db.queries import init_db, close_pool
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
)

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# Бизнес-константы. Берём из config, если он импортируется на Railway,
# иначе — из env/значений по умолчанию (реф-бонус по умолчанию 0, чтобы не начислить лишнего).
try:
    from config import MARKET_FEE, REFERRAL_BONUS_PERCENT
except Exception:
    MARKET_FEE = float(os.getenv("MARKET_FEE", "0.03"))
    REFERRAL_BONUS_PERCENT = float(os.getenv("REFERRAL_BONUS_PERCENT", "0"))
@asynccontextmanager
@asynccontextmanager
async def lifespan(app: FastAPI):
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
        raise HTTPException(404, "Listing not found")
    return item


class CreateListingBody(BaseModel):
    # Теперь принимаем ссылку на подарок из Telegram, а не числовой gift_id.
    gift_url: str
    price: float
    description: str = ""


@app.post("/api/listings")
async def create_listing_endpoint(
    body: CreateListingBody,
    x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Unauthorized")

    if body.price <= 0:
        raise HTTPException(400, "Price must be greater than 0")

    parsed = parse_gift_url(body.gift_url)
    if not parsed:
        raise HTTPException(400, "Invalid gift link. Expected t.me/nft/...")

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
        raise HTTPException(401, "Unauthorized")

    lst = await get_listing(listing_id)
    if not lst or lst["status"] != "active":
        raise HTTPException(400, "Listing unavailable")

    buyer_id = tg_user["id"]
    seller_id = lst["seller_id"]
    if buyer_id == seller_id:
        raise HTTPException(400, "Cannot buy your own listing")

    # Гарантируем, что покупатель есть в БД (мог открыть Mini App, не нажимая /start в боте)
    full_name = " ".join(
        p for p in [tg_user.get("first_name"), tg_user.get("last_name")] if p
    )
    buyer = await get_or_create_user(buyer_id, tg_user.get("username", ""), full_name)

    price = lst["price_ton"]
    fee = price * MARKET_FEE

    seller = await get_user(seller_id)
    ref_bonus = 0.0
    if seller and seller.get("referred_by"):
        ref_bonus = price * REFERRAL_BONUS_PERCENT

    # Демо-режим: списываем с внутреннего баланса. В проде — реальный TON-платёж.
    if buyer["balance_ton"] < price:
        raise HTTPException(
            400, f"Insufficient balance: {buyer['balance_ton']:.4f} TON, need {price:.4f}"
        )

    # Движение средств
    await update_balance(buyer_id, -price)
    seller_net = price - fee - ref_bonus
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

    # Уведомление продавцу (best-effort)
    buyer_name = tg_user.get("username") or tg_user.get("first_name") or "покупатель"
    await notify_seller(
        seller_id,
        f"🎉 <b>Ваш подарок продан!</b>\n\n"
        f"🎁 {lst['gift_name']} #{lst.get('gift_number','?')}\n"
        f"💰 Вы получили: {seller_net:.4f} TON\n"
        f"👤 Покупатель: @{buyer_name}"
    )

    return {
        "ok": True,
        "gift_name": lst["gift_name"],
        "gift_number": lst.get("gift_number", ""),
        "price": price,
        "fee": fee,
        "seller_net": seller_net,
    }


# ===== AUCTIONS =====

@app.get("/api/auctions")
async def auctions(limit: int = 20, offset: int = 0):
    items = await get_active_auctions(limit=limit, offset=offset)
    return {"auctions": items}


@app.get("/api/auctions/{auction_id}")
async def auction_detail(auction_id: int):
    item = await get_auction(auction_id)
    if not item:
        raise HTTPException(404, "Auction not found")
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
        raise HTTPException(401, "Unauthorized")
    ok = await place_bid(auction_id=auction_id, bidder_id=user["id"], amount=body.amount)
    if not ok:
        raise HTTPException(400, "Bid too low or auction ended")
    return {"ok": True}


# ===== PORTFOLIO =====

@app.get("/api/portfolio")
async def portfolio(x_telegram_init_data: Optional[str] = Header(None)):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Unauthorized")
    gifts = await get_user_gifts(user["id"])
    return {"gifts": gifts}


# ===== PROFILE =====

@app.get("/api/profile")
async def profile(x_telegram_init_data: Optional[str] = Header(None)):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Unauthorized")
    db_user = await get_user(user["id"])
    txs = await get_user_transactions(user["id"], limit=10)
    return {"user": db_user, "transactions": txs}


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
        raise HTTPException(401, "Unauthorized")
    if not ton_client.is_configured():
        raise HTTPException(503, "Escrow wallet is not configured")

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


@app.get("/api/escrow/deposit-intent")
async def deposit_intent_status(
        x_telegram_init_data: Optional[str] = Header(None),
):
    user = get_user_from_header(x_telegram_init_data or "")
    if not user:
        raise HTTPException(401, "Unauthorized")
    intent = await get_latest_intent_for_user(user["id"])
    if not intent:
        return {"status": "none"}
    return {
        "status": intent["status"],  # pending / completed
        "code": intent["code"],
        "gift_id": intent["gift_id"],
        "nft_address": intent["nft_address"],
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)