"""Проверка данных Telegram Mini App (initData).

Мини-приложение открывается внутри Telegram, и тот передаёт странице строку initData:
кто открыл, когда, подпись. Сервер обязан проверить подпись сам — иначе любой может
отправить запрос «я пользователь 123» и получить чужие данные.

Алгоритм (документация Telegram, раздел «Validating data received via the Mini App»):
  secret = HMAC_SHA256(key="WebAppData", msg=<токен бота>)
  hash   = hex(HMAC_SHA256(key=secret, msg=<все поля, кроме hash, по алфавиту, через \\n>))

Дополнительно отклоняем старые initData (auth_date): перехваченную строку нельзя
использовать бесконечно.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl


class InitDataError(ValueError):
    """initData поддельные, устаревшие или повреждённые."""


@dataclass(frozen=True)
class WebAppUser:
    id: int
    first_name: str
    last_name: str | None
    username: str | None
    language_code: str | None


@dataclass(frozen=True)
class WebAppAuth:
    user: WebAppUser
    auth_date: int
    query_id: str | None
    start_param: str | None


def _secret_key(bot_token: str) -> bytes:
    return hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()


def sign_init_data(fields: dict[str, str], bot_token: str) -> str:
    """Подписывает поля так же, как Telegram. Нужна тестам и локальной отладке страницы."""
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    return hmac.new(_secret_key(bot_token), check_string.encode(), hashlib.sha256).hexdigest()


def validate_init_data(init_data: str, bot_token: str, max_age_seconds: int = 3600) -> WebAppAuth:
    if not init_data or len(init_data) > 4096:
        raise InitDataError("initData отсутствуют или слишком длинные")

    pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=False)
    fields: dict[str, str] = {}
    for key, value in pairs:
        if key in fields:
            raise InitDataError("Повтор поля в initData")
        fields[key] = value

    received_hash = fields.pop("hash", "")
    if not received_hash:
        raise InitDataError("Нет подписи")
    expected_hash = sign_init_data(fields, bot_token)
    if not hmac.compare_digest(expected_hash, received_hash):
        raise InitDataError("Подпись не совпала")

    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError as exc:
        raise InitDataError("Некорректная дата") from exc
    age = time.time() - auth_date
    if age > max_age_seconds or age < -60:
        raise InitDataError("initData устарели — откройте приложение заново")

    try:
        raw_user = json.loads(fields.get("user", ""))
        user = WebAppUser(
            id=int(raw_user["id"]),
            first_name=str(raw_user.get("first_name", ""))[:128],
            last_name=(str(raw_user["last_name"])[:128] if raw_user.get("last_name") else None),
            username=(str(raw_user["username"])[:64] if raw_user.get("username") else None),
            language_code=(str(raw_user["language_code"])[:16] if raw_user.get("language_code") else None),
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise InitDataError("Нет данных пользователя") from exc

    return WebAppAuth(
        user=user,
        auth_date=auth_date,
        query_id=fields.get("query_id"),
        start_param=fields.get("start_param"),
    )
