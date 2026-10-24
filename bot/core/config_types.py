"""Типы и помощники настроек, общие для всех ботов.

Секреты в продакшене приходят файлами Docker secrets (/run/secrets/<имя>), а не
переменными окружения: те видны в `docker inspect`, /proc/<pid>/environ и отчётах о падениях.

Пароли не пишутся в адреса БД и Redis. В адресе стоит метка __DB_PASSWORD__ /
__REDIS_PASSWORD__, а значение подставляется из секрета при запуске. Метка именно
такая, а не ${...}: и docker compose, и .env-загрузчик подставили бы туда пустую строку.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, BeforeValidator, SecretStr
from pydantic_settings import NoDecode

SECRETS_DIR = Path("/run/secrets")


def secrets_dir() -> str | None:
    """Папка Docker secrets, если она есть (в контейнере). Локально — None, всё читается из .env."""
    return str(SECRETS_DIR) if SECRETS_DIR.is_dir() else None


def resolve_placeholders(url: str, secrets: dict[str, SecretStr | None]) -> str:
    """Подставляет пароли из секретов вместо меток __ИМЯ__ в адресе подключения."""
    for name, secret in secrets.items():
        placeholder = f"__{name}__"
        if placeholder not in url:
            continue
        if secret is None:
            raise ValueError(f"В адресе есть {placeholder}, но секрет {name.lower()} не задан")
        url = url.replace(placeholder, quote(secret.get_secret_value(), safe=""))
    return url


def _parse_id_list(value: Any) -> Any:
    """«123, 456» или «123;456» → {123, 456}. Пустая строка → пустое множество."""
    if isinstance(value, int):
        return {value}
    if isinstance(value, str):
        return {int(part) for part in value.replace(";", ",").split(",") if part.strip()}
    return value


def _check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except Exception as exc:
        raise ValueError(f"Неизвестный часовой пояс: {value!r}") from exc
    return value


# Список Telegram ID в .env: ADMIN_IDS=123456789,987654321
IdSet = Annotated[frozenset[int], NoDecode, BeforeValidator(_parse_id_list)]

# Название часового пояса из базы IANA: Asia/Tashkent, Europe/Moscow …
TimezoneName = Annotated[str, AfterValidator(_check_timezone)]
