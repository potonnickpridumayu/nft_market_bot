import os
from dotenv import load_dotenv

load_dotenv()

# ── Bot ──────────────────────────────────────────────────────────────────────
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# ── Admin ────────────────────────────────────────────────────────────────────
ADMIN_IDS: list[int] = [
    int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip().isdigit()
]

# ── Marketplace settings ─────────────────────────────────────────────────────
# Commission rate (e.g. 0.03 = 3%) — intentionally low vs competitors
MARKET_FEE: float = float(os.getenv("MARKET_FEE", "0.03"))

# Minimum listing price in TON
MIN_PRICE_TON: float = float(os.getenv("MIN_PRICE_TON", "0.1"))

# Auction defaults
AUCTION_MIN_STEP_PERCENT: float = 0.05   # 5% minimum bid increment
AUCTION_DEFAULT_DURATION_H: int = 24     # hours

# ── Referral ─────────────────────────────────────────────────────────────────
REFERRAL_BONUS_PERCENT: float = float(os.getenv("REFERRAL_BONUS_PERCENT", "0.01"))  # 1% of sale

# ── TON Connect / Payments ───────────────────────────────────────────────────
TON_WALLET: str = os.getenv("TON_WALLET", "YOUR_TON_WALLET_ADDRESS")
TON_API_KEY: str = os.getenv("TON_API_KEY", "")  # toncenter.com API key

# ── Misc ─────────────────────────────────────────────────────────────────────
ITEMS_PER_PAGE: int = 5
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///nft_market.db")
