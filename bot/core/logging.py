"""Логи: JSON для продакшена (удобно отправлять в Loki/ELK), текст — для разработки.

Правила:
- секреты (токены, ключи) вырезаются из любого текста лога;
- поля с «опасными» именами (password, token, cookie…) заменяются на [REDACTED];
- каждый апдейт и HTTP-запрос получает request_id — по нему находится вся цепочка событий.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id: ContextVar[str | None] = ContextVar("request_id", default=None)

SENSITIVE_KEYS = {
    "password", "token", "authorization", "cookie", "secret", "api_key", "refresh_token",
    "access_token", "init_data", "phone", "bot_token", "session",
}
_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}
# Любой токен Telegram-бота (в том числе чужой, например в конструкторе ботов) и ключи Anthropic
_TOKEN_PATTERNS = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}|sk-ant-[A-Za-z0-9_-]{20,}")


class SecretRedactingFilter(logging.Filter):
    """Вырезает значения секретов из сообщений — даже если секрет попал в текст исключения."""

    def __init__(self, secrets: list[str]) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret and len(secret) >= 6]

    def _clean(self, value: str) -> str:
        for secret in self._secrets:
            value = value.replace(secret, "[REDACTED]")
        return _TOKEN_PATTERNS.sub("[REDACTED]", value)

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        cleaned = self._clean(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_info:
            record.exc_text = self._clean(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def _extra_fields(record: logging.LogRecord) -> dict[str, Any]:
    fields = {}
    for key, value in record.__dict__.items():
        if key in _STANDARD_ATTRS or key.startswith("_"):
            continue
        fields[key] = "[REDACTED]" if key.lower() in SENSITIVE_KEYS else value
    return fields


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if rid := request_id.get():
            payload["request_id"] = rid
        payload.update(_extra_fields(record))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exc"] = record.exc_text
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s")

    def format(self, record: logging.LogRecord) -> str:
        line = super().format(record)
        extra = _extra_fields(record)
        if rid := request_id.get():
            extra["request_id"] = rid
        return f"{line} {extra}" if extra else line


def setup_logging(level: str = "INFO", secrets: list[str] | None = None, json_logs: bool = False) -> None:
    with contextlib.suppress(AttributeError, ValueError):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")  # type: ignore[union-attr]

    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter() if json_logs else TextFormatter())
    handler.addFilter(SecretRedactingFilter(secrets or []))

    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("httpx", "httpx2", "httpcore", "aiohttp.access", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
