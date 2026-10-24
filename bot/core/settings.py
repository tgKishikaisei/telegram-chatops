"""Базовые настройки, общие для всех ботов. Настройки конкретного бота наследуют BaseBotSettings.

Откуда берутся значения (по убыванию приоритета): переменные окружения → файлы
/run/secrets/<имя_поля> → файл .env → значения по умолчанию.
"""

from __future__ import annotations

from typing import Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from bot.core.config_types import IdSet, resolve_placeholders, secrets_dir


class DatabaseSettings(BaseSettings):
    """Только подключение к БД — этого достаточно миграциям (им не нужен токен бота)."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", secrets_dir=secrets_dir(), extra="ignore"
    )

    database_url: str = "sqlite+aiosqlite:///data/bot.db"
    db_password: SecretStr | None = None
    db_pool_size: int = 5

    @property
    def database_dsn(self) -> str:
        return resolve_placeholders(self.database_url, {"DB_PASSWORD": self.db_password})


class BaseBotSettings(DatabaseSettings):
    bot_token: SecretStr
    environment: Literal["development", "production"] = "development"

    redis_url: str | None = None
    redis_password: SecretStr | None = None

    admin_ids: IdSet = frozenset()
    log_level: str = "INFO"
    json_logs: bool = False

    @property
    def redis_dsn(self) -> str | None:
        if not self.redis_url:
            return None
        return resolve_placeholders(self.redis_url, {"REDIS_PASSWORD": self.redis_password})

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def secret_values(self) -> list[str]:
        """Значения всех секретов — фильтр логов вырежет их, где бы они ни встретились."""
        values = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                values.append(value.get_secret_value())
        return values

    @model_validator(mode="after")
    def _production_guards(self) -> BaseBotSettings:
        if self.is_production:
            if self.database_url.startswith("sqlite"):
                raise ValueError("В продакшене нужен PostgreSQL: SQLite не рассчитан на несколько процессов")
            if not self.redis_url:
                raise ValueError("В продакшене нужен Redis: лимиты и диалоги должны переживать перезапуск")
        return self
