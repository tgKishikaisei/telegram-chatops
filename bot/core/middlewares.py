"""Общие middleware: request_id, сессия БД на каждый апдейт и защита от флуда."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update, User
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.core.logging import request_id
from bot.core.ratelimit import RateLimiter

Handler = Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]]
logger = logging.getLogger(__name__)


class RequestIdMiddleware(BaseMiddleware):
    """Каждый апдейт получает метку — все строки лога одной обработки связаны между собой."""

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        label = f"upd-{event.update_id}" if isinstance(event, Update) else uuid.uuid4().hex[:12]
        token = request_id.set(label)
        try:
            return await handler(event, data)
        finally:
            request_id.reset(token)


class DbSessionMiddleware(BaseMiddleware):
    """Открывает сессию БД на время обработки апдейта и передаёт её в хендлер аргументом `session`.

    Хендлер завершился без ошибки — изменения сохраняются (commit).
    Хендлер упал — всё, что он успел изменить, откатывается автоматически.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        async with self._session_factory() as session:
            data["session"] = session
            result = await handler(event, data)
            await session.commit()
            return result


class ThrottlingMiddleware(BaseMiddleware):
    """Не больше `limit` событий за `period` секунд от одного пользователя.

    Счётчики в Redis — лимит общий для всех экземпляров бота и переживает перезапуск.
    Лишнее молча отбрасывается, кнопкам отвечаем коротким уведомлением.
    Подтверждения платежей не ограничиваются никогда: потерять оплату хуже любого флуда.
    """

    def __init__(self, limiter: RateLimiter, limit: int = 5, period: int = 3) -> None:
        self._limiter = limiter
        self._limit = limit
        self._period = period

    async def __call__(self, handler: Handler, event: TelegramObject, data: dict[str, Any]) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or (isinstance(event, Message) and event.successful_payment):
            return await handler(event, data)

        if not await self._limiter.hit(f"user:{user.id}", self._limit, self._period):
            if isinstance(event, CallbackQuery):
                await event.answer("Слишком часто — подождите пару секунд")
            return None
        return await handler(event, data)
