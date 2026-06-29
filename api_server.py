"""FastAPI сервер для GiftSafe Mini App"""
import hmac
import hashlib
import json
from typing import Optional
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from db.queries import (
    get_active_listings, get_listing, get_active_auctions,
    get_auction, place_bid, get_user_gifts, get_user,
    get_or_create_user, create_listing, add_gift,
    get_user_transactions, get_platform_stats
)
import os
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")

app = FastAPI(title="GiftSafe API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def verify_telegram_data(init_data: str) -> Optional[dict]:
    """Верифицируем initData от Telegram WebApp"""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        params = dict(p.split("=", 1) for p in init_data.split("&") if "=" in p)
        hash_val = params.pop("hash", "")
        data_str = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        check = hmac.new(secret, data_str.encode(), hashlib.sha256).hexdigest()
        if check != hash_val:
            return None
        user_str = params.get("user", "{}")
        return json.loads(user_str)
    except Exception:
        return None


def get_user_from_header(x_telegram_init_data: str = "") -> Optional[dict]:
    if not x_telegram_init_data:
        return None
    return verify_telegram_data(x_telegram_init_data)


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
    uvicorn.run(app, host="0.0.0.0", port=8000)
