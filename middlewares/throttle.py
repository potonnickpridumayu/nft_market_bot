import asyncio
from typing import Any, Awaitable, Callable, Dict
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Message, CallbackQuery
from cachetools import TTLCache


class ThrottlingMiddleware(BaseMiddleware):
    """Simple per-user rate limiter."""

    def __init__(self, rate_limit: float = 0.5):
        self.rate_limit = rate_limit
        self.cache: TTLCache = TTLCache(maxsize=10_000, ttl=rate_limit)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user:
            key = f"throttle:{user.id}"
            if key in self.cache:
                if isinstance(event, CallbackQuery):
                    await event.answer("⏳ Не так быстро!", show_alert=False)
                return
            self.cache[key] = True
        return await handler(event, data)
