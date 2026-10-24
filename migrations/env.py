"""Alembic: миграции схемы БД.

Запуск: alembic upgrade head. В Docker это делает отдельный сервис migrate под ролью-владельцем
схемы (bot_owner); сам бот работает под ролью bot_app, у которой нет прав менять схему.
Адрес БД берётся из тех же настроек, что и у бота (DATABASE_URL + секрет db_password).
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from bot import models  # noqa: F401 — регистрирует все таблицы в Base.metadata
from bot.core.database import Base
from bot.core.settings import DatabaseSettings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    return DatabaseSettings().database_dsn


def run_migrations_offline() -> None:
    """Печатает SQL вместо выполнения: alembic upgrade head --sql (удобно для ревью DBA)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",  # SQLite не умеет ALTER COLUMN
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url())
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
