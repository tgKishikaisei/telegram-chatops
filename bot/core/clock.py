"""Время. Правило проекта: в БД время хранится в UTC без tzinfo, показывается — в поясе пользователя."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    """Текущий момент в UTC без tzinfo — в таком виде время пишется в БД."""
    return datetime.now(UTC).replace(tzinfo=None)


def to_local(moment_utc: datetime, tz: ZoneInfo) -> datetime:
    """Время из БД (naive UTC) → время в нужном часовом поясе."""
    return moment_utc.replace(tzinfo=UTC).astimezone(tz)


def to_utc(moment: datetime) -> datetime:
    """Время с часовым поясом → naive UTC для записи в БД."""
    return moment.astimezone(UTC).replace(tzinfo=None)
