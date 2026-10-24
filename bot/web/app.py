"""Приём вебхуков: POST /hooks/alerts (Alertmanager, Grafana, свои скрипты) и POST /hooks/github.

Подлинность проверяется ДО разбора и обращения к БД. Ответ — сразу: инцидент записывается в БД,
а в Telegram его публикует фоновая задача бота (outbox) — сбой Telegram не теряет алерт.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from bot import models  # noqa: F401 — регистрирует таблицы
from bot.config import Settings
from bot.core import database
from bot.core import redis as redis_core
from bot.core.clock import utcnow
from bot.core.logging import setup_logging
from bot.core.ratelimit import RateLimiter
from bot.core.web import AppError, client_ip, create_app
from bot.repositories import ops as repo
from bot.services import incidents, webhooks

logger = logging.getLogger(__name__)
PREFIX = "chatops"


def create_web_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    setup_logging(settings.log_level, settings.secret_values(), settings.json_logs)
    engine = database.create_engine(settings.database_dsn, settings.db_pool_size)
    state: dict[str, object] = {}

    async def db_ready() -> bool:
        return await database.ping(engine)

    async def redis_ready() -> bool:
        return await redis_core.ping(state.get("redis"))  # type: ignore[arg-type]

    app = create_app(title="ChatOps hooks", allowed_hosts=[settings.public_host, "localhost", "127.0.0.1"],
                     production=settings.is_production, max_body_bytes=512 * 1024,
                     readiness_checks={"database": db_ready, "redis": redis_ready})

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if not settings.is_production:
            await database.create_tables(engine)
        redis = await redis_core.create_redis(settings.redis_dsn)
        state["redis"] = redis
        app.state.session_factory = database.create_session_factory(engine)
        app.state.limiter = RateLimiter(redis, PREFIX)
        try:
            yield
        finally:
            if redis is not None:
                await redis.aclose()
            await engine.dispose()

    app.router.lifespan_context = lifespan

    async def _limited(request: Request) -> None:
        if not await request.app.state.limiter.hit(f"hook:{client_ip(request)}", 300, 60):
            raise AppError(429, "too_many_requests", "Слишком много запросов")

    def _json(body: bytes) -> object:
        try:
            return json.loads(body)
        except ValueError as exc:
            raise AppError(400, "bad_request", "Некорректный JSON") from exc

    @app.post("/hooks/alerts", include_in_schema=False)
    async def alerts_hook(request: Request) -> JSONResponse:
        if not webhooks.check_bearer(request.headers.get("authorization"),
                                     settings.alert_webhook_secret.get_secret_value()):
            raise AppError(401, "unauthorized", "Неверный токен")
        await _limited(request)
        events = webhooks.parse_alerts(_json(await request.body()))
        outcomes = []
        async with request.app.state.session_factory() as session:
            for event in events:
                outcomes.append((await incidents.ingest(session, event, "alert", utcnow())).outcome)
            await session.commit()
        return JSONResponse({"ok": True, "processed": len(outcomes)})

    @app.post("/hooks/github", include_in_schema=False)
    async def github_hook(request: Request) -> JSONResponse:
        body = await request.body()
        if not webhooks.verify_github(body, request.headers.get("x-hub-signature-256"),
                                      settings.github_webhook_secret.get_secret_value()):
            raise AppError(401, "unauthorized", "Неверная подпись")
        await _limited(request)
        event = request.headers.get("x-github-event", "")
        delivery = request.headers.get("x-github-delivery", "")
        if event == "ping":
            return JSONResponse({"ok": True})
        parsed = webhooks.parse_github(event, _json(body))
        async with request.app.state.session_factory() as session:
            if delivery and not await repo.record_delivery(session, "github", delivery, utcnow()):
                return JSONResponse({"ok": True, "duplicate": True})  # повтор доставки
            if parsed.alert is not None:
                await incidents.ingest(session, parsed.alert, "github", utcnow())
            if parsed.notice:
                await repo.add_notice(session, parsed.notice, utcnow())
            await session.commit()
        return JSONResponse({"ok": True})

    return app
