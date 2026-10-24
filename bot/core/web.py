"""Веб-слой (FastAPI) для мини-приложений и вебхуков — с безопасными настройками по умолчанию.

Что включено:
- проверка заголовка Host (TrustedHost) — защита от подмены хоста;
- заголовки безопасности (CSP, nosniff, Referrer-Policy, запрет кэширования API);
- ограничение размера тела запроса;
- request_id в каждом ответе и в логах;
- единый формат ошибок {"error": {"code", "message", "request_id"}} без стека и без
  эха присланных данных (в них могут быть персональные данные);
- /health (процесс жив) и /ready (БД и Redis доступны);
- документация API (/docs, /openapi.json) выключена в продакшене.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bot.core.logging import request_id
from bot.core.telegram_webapp import InitDataError, WebAppAuth, validate_init_data

logger = logging.getLogger("bot.web")

ReadinessCheck = Callable[[], Awaitable[bool]]

API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"
# Мини-приложение открывается во фрейме веб-версии Telegram — только её и разрешаем как «родителя»
MINI_APP_CSP = (
    "default-src 'self'; script-src 'self' https://telegram.org; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors https://web.telegram.org https://webk.telegram.org https://webz.telegram.org"
)
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")


class AppError(Exception):
    """Ожидаемая ошибка API: код и текст уходят клиенту как есть."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(
        {"error": {"code": code, "message": message, "request_id": request_id.get()}},
        status_code=status,
        headers=headers,
    )


class RequestContextMiddleware:
    """request_id + короткая строка лога на каждый запрос (без query string — там бывают токены)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        rid = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        token = request_id.set(rid)
        started = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", rid.encode()))
            await send(message)

        started_response = False

        async def tracking_send(message: Message) -> None:
            nonlocal started_response
            if message["type"] == "http.response.start":
                started_response = True
            await send_wrapper(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            # Непредвиденная ошибка: стек — в лог, клиенту — общий текст с request_id
            logger.error("Необработанная ошибка", exc_info=exc)
            if started_response:
                raise
            status = 500
            response = _error(500, "internal_error", "Внутренняя ошибка сервера",
                              headers={"x-content-type-options": "nosniff", "cache-control": "no-store"})
            await response(scope, receive, send_wrapper)
        finally:
            logger.info(
                "%s %s → %s", scope["method"], scope["path"], status,
                extra={"duration_ms": round((time.perf_counter() - started) * 1000, 1)},
            )
            request_id.reset(token)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp, page_csp: str = MINI_APP_CSP) -> None:
        self.app = app
        self.page_csp = page_csp

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_api = scope["path"].startswith("/api/")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.extend([
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                    (b"content-security-policy", (API_CSP if is_api else self.page_csp).encode()),
                ])
                if is_api:
                    headers.append((b"cache-control", b"no-store"))
            await send(message)

        await self.app(scope, receive, send_wrapper)


class BodySizeLimitMiddleware:
    """Отклоняет тела больше лимита — и по Content-Length, и по фактически прочитанным байтам."""

    def __init__(self, app: ASGIApp, max_bytes: int = 64 * 1024) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.max_bytes):
            await _error(413, "payload_too_large", "Слишком большой запрос")(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise AppError(413, "payload_too_large", "Слишком большой запрос")
            return message

        await self.app(scope, limited_receive, send)


def create_app(
    *,
    title: str,
    allowed_hosts: list[str],
    production: bool,
    readiness_checks: dict[str, ReadinessCheck] | None = None,
    max_body_bytes: int = 64 * 1024,
    page_csp: str = MINI_APP_CSP,
) -> FastAPI:
    app = FastAPI(
        title=title,
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )
    checks = readiness_checks or {}

    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "Ошибка запроса"
        return _error(exc.status_code, "http_error", message, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # Только где ошибка, без присланных значений
        fields = sorted({".".join(str(part) for part in error.get("loc", ())) for error in exc.errors()})
        return _error(422, "validation_error", "Проверьте поля: " + ", ".join(fields)[:300])

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready", include_in_schema=False)
    async def ready() -> JSONResponse:
        results = {name: await check() for name, check in checks.items()}
        ok = all(results.values())
        return JSONResponse({"status": "ok" if ok else "degraded", "checks": results}, status_code=200 if ok else 503)

    # Порядок: последний добавленный выполняется первым.
    # RequestContext — самый внешний: он ловит непредвиденные ошибки и отвечает с request_id.
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body_bytes)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts or ["localhost", "127.0.0.1"])
    app.add_middleware(SecurityHeadersMiddleware, page_csp=page_csp)
    app.add_middleware(RequestContextMiddleware)
    return app


class TelegramAuth:
    """Зависимость FastAPI: проверяет заголовок `Authorization: tma <initData>` и возвращает пользователя."""

    def __init__(self, bot_token: str, max_age_seconds: int = 3600) -> None:
        self._bot_token = bot_token
        self._max_age = max_age_seconds

    async def __call__(self, request: Request) -> WebAppAuth:
        header = request.headers.get("authorization", "")
        scheme, _, init_data = header.partition(" ")
        if scheme.lower() != "tma" or not init_data:
            raise AppError(401, "unauthorized", "Откройте приложение из Telegram")
        try:
            return validate_init_data(init_data, self._bot_token, self._max_age)
        except InitDataError as exc:
            raise AppError(401, "unauthorized", str(exc)) from exc


def client_ip(request: Request) -> str:
    """IP клиента. За Caddy uvicorn запускается с --proxy-headers и доверяет только ему."""
    return request.client.host if request.client else "unknown"


def json_body_fields(data: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    """Белый список полей: лишние поля из запроса не попадут в модель (защита от mass assignment)."""
    return {key: value for key, value in data.items() if key in allowed}
