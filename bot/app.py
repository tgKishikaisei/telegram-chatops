"""Сборка бота (вебхуки принимает отдельный процесс python -m bot.web): настройки → БД и Redis →
администраторы → каталог действий и агент → диспетчер → задачи (посты, эскалация) → запуск."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from functools import partial

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot import models  # noqa: F401 — регистрирует таблицы
from bot.config import Settings
from bot.core.clock import utcnow
from bot.core.database import create_engine, create_session_factory, create_tables
from bot.core.errors import register_error_handler
from bot.core.heartbeat import run_heartbeat
from bot.core.logging import setup_logging
from bot.core.middlewares import DbSessionMiddleware, RequestIdMiddleware, ThrottlingMiddleware
from bot.core.ratelimit import RateLimiter
from bot.core.redis import create_fsm_storage, create_redis
from bot.core.scheduler import PeriodicJobs
from bot.gateways.agent import AgentClient
from bot.handlers import COMMANDS, get_routers
from bot.handlers.ops import MemberMiddleware
from bot.jobs.ops import escalate, housekeeping, send_notices, sync_posts
from bot.models import Role
from bot.repositories import ops as ops_repo
from bot.services.runbooks import load_catalog

logger = logging.getLogger(__name__)

PREFIX = "chatops"


async def main() -> None:
    settings = Settings()
    setup_logging(settings.log_level, settings.secret_values(), settings.json_logs)

    engine = create_engine(settings.database_dsn, settings.db_pool_size)
    if not settings.is_production:
        await create_tables(engine)  # в продакшене схему создаёт alembic upgrade head
    session_factory = create_session_factory(engine)
    async with session_factory() as session:  # ADMIN_IDS — администраторы с первого запуска
        for admin_id in settings.admin_ids:
            await ops_repo.ensure_member(session, admin_id, str(admin_id), Role.ADMIN, utcnow())
        await session.commit()
    catalog = load_catalog()  # ошибка в actions.json — бот не стартует (лучше, чем действие «не туда»)
    agent = AgentClient(catalog.agent_url, settings.agent_secret.get_secret_value())

    redis = await create_redis(settings.redis_dsn)
    limiter = RateLimiter(redis, PREFIX)
    bot = Bot(settings.bot_token.get_secret_value(), default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    dispatcher = Dispatcher(storage=create_fsm_storage(redis, PREFIX), settings=settings, limiter=limiter,
                            catalog=catalog, agent=agent)
    dispatcher.update.outer_middleware(RequestIdMiddleware())
    dispatcher.update.middleware(DbSessionMiddleware(session_factory))
    dispatcher.message.middleware(ThrottlingMiddleware(limiter, limit=5, period=3))
    dispatcher.callback_query.middleware(ThrottlingMiddleware(limiter, limit=6, period=3))
    dispatcher.message.middleware(MemberMiddleware())
    dispatcher.callback_query.middleware(MemberMiddleware())
    dispatcher.include_routers(*get_routers())
    register_error_handler(dispatcher)

    jobs = PeriodicJobs(redis, PREFIX)
    jobs.add("sync_posts", 3, partial(sync_posts, bot, session_factory, settings))
    jobs.add("send_notices", 5, partial(send_notices, bot, session_factory, settings))
    jobs.add("escalate", 30, partial(escalate, bot, session_factory, settings))
    jobs.add("housekeeping", 60, partial(housekeeping, session_factory, settings))
    heartbeat = asyncio.create_task(run_heartbeat())

    try:
        await bot.set_my_commands(COMMANDS)
        jobs.start()
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        await jobs.stop()
        heartbeat.cancel()
        await bot.session.close()
        await agent.close()
        with contextlib.suppress(Exception):
            await dispatcher.storage.close()
        if redis is not None:
            with contextlib.suppress(Exception):
                await redis.aclose()
        await engine.dispose()
