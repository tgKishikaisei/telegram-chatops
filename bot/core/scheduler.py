"""Фоновые задачи: цикл «выполнить → подождать» поверх asyncio.

Если запущено несколько экземпляров бота, одну задачу в каждый момент выполняет только один:
перед запуском экземпляр занимает ключ в Redis (SET NX с истечением), после — освобождает
(только свой ключ: проверка и удаление в одной транзакции WATCH/MULTI). Истечение страхует
от зависшего процесса. Сами задачи при этом всё равно идемпотентны — каждую запись они
«забирают» атомарным UPDATE, так что даже двойной запуск ничего не сломает.

Что и кому отправлять, всегда решается по данным в БД, поэтому перезапуск
бота ничего не теряет: следующий проход подхватит всё, что не успели.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from redis.asyncio import Redis
from redis.exceptions import WatchError

from bot.core.heartbeat import beat
from bot.core.logging import request_id

logger = logging.getLogger(__name__)

Job = Callable[[], Awaitable[None]]


class PeriodicJobs:
    def __init__(self, redis: Redis | None = None, prefix: str = "bot") -> None:
        self._jobs: list[tuple[str, float, Job, float]] = []
        self._tasks: list[asyncio.Task[None]] = []
        self._redis = redis
        self._prefix = prefix
        self._instance = uuid.uuid4().hex

    def add(self, name: str, every_seconds: float, job: Job, lock_seconds: float | None = None) -> None:
        """lock_seconds — сколько максимум может длиться задача (по умолчанию 2 минуты)."""
        self._jobs.append((name, every_seconds, job, lock_seconds or 120))

    def start(self) -> None:
        for name, every_seconds, job, lock_seconds in self._jobs:
            task = asyncio.create_task(self._run_forever(name, every_seconds, job, lock_seconds), name=f"job:{name}")
            self._tasks.append(task)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _acquire(self, key: str, lock_seconds: float) -> bool:
        if self._redis is None:
            return True
        try:
            return bool(await self._redis.set(key, self._instance, nx=True, px=int(lock_seconds * 1000)))
        except Exception:
            logger.warning("Redis недоступен — задача %s выполняется без блокировки", key)
            return True

    async def _release(self, key: str) -> None:
        if self._redis is None:
            return
        try:
            async with self._redis.pipeline(transaction=True) as pipe:
                await pipe.watch(key)
                if await pipe.get(key) == self._instance:
                    pipe.multi()
                    pipe.delete(key)
                    await pipe.execute()
                else:
                    await pipe.unwatch()
        except WatchError:
            pass  # ключ изменился — значит, он уже не наш
        except Exception:
            logger.warning("Не удалось освободить блокировку %s — истечёт сама", key)

    async def run_once(self, name: str, job: Job, lock_seconds: float = 120) -> bool:
        """Выполнить задачу один раз под блокировкой. False — её сейчас выполняет другой экземпляр."""
        key = f"{self._prefix}:job:{name}"
        if not await self._acquire(key, lock_seconds):
            return False
        try:
            await job()
        finally:
            await self._release(key)
        return True

    async def _run_forever(self, name: str, every_seconds: float, job: Job, lock_seconds: float) -> None:
        while True:
            token = request_id.set(f"job-{name}-{uuid.uuid4().hex[:6]}")
            try:
                await self.run_once(name, job, lock_seconds)
                beat()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Одна ошибка не должна останавливать задачу навсегда
                logger.exception("Фоновая задача %s упала, повторим через %s с", name, every_seconds)
            finally:
                request_id.reset(token)
            await asyncio.sleep(every_seconds)
