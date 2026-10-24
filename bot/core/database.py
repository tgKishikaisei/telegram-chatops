"""База данных: движок, сессии, базовый класс таблиц.

Продакшен — PostgreSQL (схему создаёт Alembic под ролью-владельцем, приложение работает
под ролью без прав на DDL). Локально и в тестах — SQLite без установки.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, event, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

# Предсказуемые имена ограничений — без них Alembic не сможет корректно их удалять и переименовывать
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def create_engine(database_url: str, pool_size: int = 5) -> AsyncEngine:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite":
        # pool_pre_ping — переживаем перезапуск PostgreSQL; размер пула — явно, а не «по умолчанию»
        return create_async_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=pool_size)

    if url.database and url.database != ":memory:":
        Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(url, connect_args={"timeout": 15})

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _record) -> None:  # type: ignore[no-untyped-def]
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine: AsyncEngine) -> None:
    """Только для разработки и тестов. В продакшене схему ведёт Alembic (alembic upgrade head)."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def ping(engine: AsyncEngine) -> bool:
    """Проверка готовности для /ready и healthcheck."""
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def insert_or_ignore(session: AsyncSession, model: type[Base], values: dict[str, Any],
                           conflict_columns: list[str] | None) -> bool:
    """INSERT … ON CONFLICT DO NOTHING. True — строка вставлена, False — такая уже была.

    Это атомарная защита от повторов («бонус выдаётся один раз», «промокод — один на человека»):
    два одновременных запроса не смогут вставить дубль, а проигравший просто получит False,
    не ломая транзакцию (в отличие от перехвата IntegrityError).
    conflict_columns=None — игнорировать конфликт по любому уникальному ключу.
    """
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    statement = insert(model).values(**values).on_conflict_do_nothing(index_elements=conflict_columns)
    result = await session.execute(statement)
    return bool(result.rowcount)


async def upsert(session: AsyncSession, model: type[Base], values: dict[str, Any], conflict_columns: list[str],
                 update_columns: list[str]) -> None:
    """INSERT … ON CONFLICT DO UPDATE: «создать или заменить» одной атомарной командой (например, переголосовать)."""
    insert = pg_insert if session.get_bind().dialect.name == "postgresql" else sqlite_insert
    statement = insert(model).values(**values)
    statement = statement.on_conflict_do_update(
        index_elements=conflict_columns,
        set_={column: statement.excluded[column] for column in update_columns},
    )
    await session.execute(statement)
