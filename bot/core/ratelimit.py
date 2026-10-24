"""Атомарный ограничитель частоты.

Счётчик увеличивается ДО проверки (INCR + EXPIRE NX в одной транзакции Redis). Схема
«прочитать → проверить → записать» ломается пачкой одновременных запросов: все прочитают ноль.
Здесь каждый запрос получает свой номер 1…N, и лишние отсекаются точно по лимиту.
"""

from __future__ import annotations

import time

from redis.asyncio import Redis


class RateLimiter:
    def __init__(self, redis: Redis | None, prefix: str) -> None:
        self._redis = redis
        self._prefix = prefix
        self._memory: dict[str, tuple[float, int]] = {}

    async def hit(self, key: str, limit: int, window_seconds: int) -> bool:
        """Засчитывает попытку. True — в пределах лимита, False — лимит превышен."""
        return await self.count(key, window_seconds) <= limit

    async def count(self, key: str, window_seconds: int) -> int:
        full_key = f"{self._prefix}:rl:{key}"
        if self._redis is not None:
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.incr(full_key)
                pipe.expire(full_key, window_seconds, nx=True)
                attempts, _ = await pipe.execute()
            return int(attempts)
        return self._memory_count(full_key, window_seconds)

    async def reset(self, key: str) -> None:
        full_key = f"{self._prefix}:rl:{key}"
        if self._redis is not None:
            await self._redis.delete(full_key)
        self._memory.pop(full_key, None)

    def _memory_count(self, key: str, window_seconds: int) -> int:
        now = time.monotonic()
        started, attempts = self._memory.get(key, (now, 0))
        if now - started >= window_seconds:
            started, attempts = now, 0
        attempts += 1
        self._memory[key] = (started, attempts)
        if len(self._memory) > 50_000:  # не даём словарю расти бесконечно
            self._memory = {k: v for k, v in self._memory.items() if now - v[0] < window_seconds}
        return attempts
