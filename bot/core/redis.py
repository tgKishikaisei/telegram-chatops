"""Redis: общие счётчики, блокировки и состояние диалогов для нескольких экземпляров бота.

REDIS_URL не задан → бот работает и без Redis (всё в памяти процесса) — удобно локально.
В продакшене Redis закрыт паролем через ACL и доступен только из внутренней сети (см. deploy/).
"""

from __future__ import annotations

import logging

from aiogram.fsm.storage.base import BaseStorage, DefaultKeyBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

DIALOG_TTL_SECONDS = 24 * 3600  # незавершённые диалоги (анкеты, оформление) живут сутки — не храним лишнего


async def create_redis(url: str | None) -> Redis | None:
    if not url:
        logger.warning("REDIS_URL не задан — лимиты и состояния диалогов живут в памяти процесса")
        return None
    client = Redis.from_url(url, decode_responses=True, socket_timeout=5, socket_connect_timeout=5,
                            health_check_interval=30)
    await client.ping()
    return client


def create_fsm_storage(redis: Redis | None, prefix: str) -> BaseStorage:
    if redis is None:
        return MemoryStorage()
    return RedisStorage(
        redis=redis,
        key_builder=DefaultKeyBuilder(prefix=f"{prefix}:fsm", with_destiny=True),
        state_ttl=DIALOG_TTL_SECONDS,
        data_ttl=DIALOG_TTL_SECONDS,
    )


async def ping(redis: Redis | None) -> bool:
    if redis is None:
        return True
    try:
        return bool(await redis.ping())
    except Exception:
        return False
