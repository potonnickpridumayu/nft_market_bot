import asyncio
import logging
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from config import BOT_TOKEN
from db.database import init_db
from handlers import start, market, sell, auction, portfolio, referral, admin
from middlewares.throttle import ThrottlingMiddleware
from utils.scheduler import run_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger(__name__)


async def main():
    await init_db()

    session = AiohttpSession(proxy="http://127.0.0.1:12334")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        session=session
    )

    dp = Dispatcher(storage=MemoryStorage())

    dp.message.middleware(ThrottlingMiddleware(rate_limit=0.5))
    dp.callback_query.middleware(ThrottlingMiddleware(rate_limit=0.3))

    dp.include_router(start.router)
    dp.include_router(market.router)
    dp.include_router(sell.router)
    dp.include_router(auction.router)
    dp.include_router(portfolio.router)
    dp.include_router(referral.router)
    dp.include_router(admin.router)

    logger.info("NFT Market Bot starting...")
    await bot.delete_webhook(drop_pending_updates=True)
    await asyncio.gather(
        dp.start_polling(bot),
        run_scheduler(bot),
    )


if __name__ == "__main__":
    asyncio.run(main())
