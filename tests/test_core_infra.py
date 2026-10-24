"""Тесты общего ядра: лимиты, блокировки фоновых задач, секреты в настройках, логи."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from pydantic import SecretStr

from bot.core.config_types import resolve_placeholders
from bot.core.logging import JsonFormatter, SecretRedactingFilter, request_id
from bot.core.ratelimit import RateLimiter
from bot.core.scheduler import PeriodicJobs


async def test_rate_limiter_is_exact_under_concurrency(redis):
    limiter = RateLimiter(redis, "t")
    results = await asyncio.gather(*(limiter.hit("user:1", 5, 60) for _ in range(20)))
    assert results.count(True) == 5  # ровно лимит, даже при одновременных запросах


async def test_rate_limiter_keys_are_independent_and_resettable(redis):
    limiter = RateLimiter(redis, "t")
    assert await limiter.hit("a", 1, 60)
    assert not await limiter.hit("a", 1, 60)
    assert await limiter.hit("b", 1, 60)
    await limiter.reset("a")
    assert await limiter.hit("a", 1, 60)


async def test_rate_limiter_sets_ttl(redis):
    limiter = RateLimiter(redis, "t")
    await limiter.hit("x", 3, 30)
    assert 0 < await redis.ttl("t:rl:x") <= 30


async def test_rate_limiter_without_redis():
    limiter = RateLimiter(None, "t")
    assert [await limiter.hit("k", 2, 60) for _ in range(3)] == [True, True, False]


async def test_job_lock_prevents_parallel_runs(redis):
    first, second = PeriodicJobs(redis, "t"), PeriodicJobs(redis, "t")
    started = asyncio.Event()
    release = asyncio.Event()
    runs: list[str] = []

    async def slow_job() -> None:
        runs.append("run")
        started.set()
        await release.wait()

    task = asyncio.create_task(first.run_once("job", slow_job))
    await started.wait()
    assert await second.run_once("job", slow_job) is False  # первый ещё работает
    release.set()
    assert await task is True
    assert await second.run_once("job", slow_job) is True  # блокировка освобождена
    assert runs == ["run", "run"]


async def test_job_lock_released_after_error(redis):
    jobs = PeriodicJobs(redis, "t")

    async def failing() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await jobs.run_once("job", failing)
    assert await redis.get("t:job:job") is None


async def test_job_lock_does_not_release_foreign_key(redis):
    jobs = PeriodicJobs(redis, "t")

    async def steal() -> None:
        await redis.set("t:job:job", "someone-else")

    await jobs.run_once("job", steal)
    assert await redis.get("t:job:job") == "someone-else"


def test_placeholders_are_url_encoded():
    url = resolve_placeholders("postgresql+asyncpg://app:__DB_PASSWORD__@db/x", {"DB_PASSWORD": SecretStr("p@ss/w:rd")})
    assert url == "postgresql+asyncpg://app:p%40ss%2Fw%3Ard@db/x"


def test_placeholder_without_secret_fails():
    with pytest.raises(ValueError):
        resolve_placeholders("redis://bot:__REDIS_PASSWORD__@redis", {"REDIS_PASSWORD": None})


def test_url_without_placeholder_is_unchanged():
    assert resolve_placeholders("sqlite+aiosqlite:///data/bot.db", {"DB_PASSWORD": None}) == "sqlite+aiosqlite:///data/bot.db"


def _record(message: str, *args: object, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, message, args, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_secrets_are_redacted_from_messages():
    record = _record("token is %s", "123456:SECRET-TOKEN")
    SecretRedactingFilter(["123456:SECRET-TOKEN"]).filter(record)
    assert "SECRET" not in record.getMessage()


def test_json_log_hides_sensitive_fields_and_adds_request_id():
    token = request_id.set("req-1")
    try:
        line = JsonFormatter().format(_record("hello", password="hunter2", user=5))
    finally:
        request_id.reset(token)
    payload = json.loads(line)
    assert payload["password"] == "[REDACTED]"
    assert payload["user"] == 5
    assert payload["request_id"] == "req-1"


def test_any_bot_token_redacted_even_if_unknown():
    fake_token = "7123456789:" + "AAH-" + "abcdefghijklmnopqrstuvwxyz0123456"  # склейка: сканеры секретов не шумят
    record = _record(f"child bot {fake_token} failed")
    SecretRedactingFilter([]).filter(record)
    assert "AAH-" not in record.getMessage() and "[REDACTED]" in record.getMessage()
