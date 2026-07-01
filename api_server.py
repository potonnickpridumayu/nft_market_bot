"""FastAPI сервер для GiftSafe Mini App"""
import hmac
import hashlib
import json
import os
import asyncio
import urllib.request
from typing import Optional
from urllib.parse import parse_qsl

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
    record_referral_payout, mark_listing_sold,
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

app = FastAPI(title="GiftSafe API")

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
    gift_id: int
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
    listing_id = await create_listing(
        gift_id=body.gift_id,
        seller_id=user["id"],
        price_ton=body.price,
        description=body.description,
    )
    return {"ok": True, "listing_id": listing_id}


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


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)