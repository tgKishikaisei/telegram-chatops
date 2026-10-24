"""Общие фикстуры тестов: чистая БД в памяти и поддельный Redis для каждого теста."""

import pytest
from fakeredis import FakeAsyncRedis

from bot import models  # noqa: F401 — регистрирует все таблицы
from bot.core.database import Base, create_engine, create_session_factory


@pytest.fixture
async def engine():
    engine = create_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def session_factory(engine):
    return create_session_factory(engine)


@pytest.fixture
async def session(session_factory):
    async with session_factory() as db_session:
        yield db_session


@pytest.fixture
async def file_session_factory(tmp_path):
    """БД в файле: у каждой сессии своё соединение. Нужна для тестов гонок — в «:memory:» все сессии
    делят одно соединение, и откат одной стирает незакоммиченное другой."""
    engine = create_engine(f"sqlite+aiosqlite:///{(tmp_path / 'race.db').as_posix()}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def redis():
    client = FakeAsyncRedis(decode_responses=True)
    yield client
    await client.aclose()
