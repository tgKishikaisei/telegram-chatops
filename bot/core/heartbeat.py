"""Проверка «бот жив» для Docker HEALTHCHECK.

У бота на long polling нет HTTP-порта, поэтому живость проверяется по файлу-пульсу:
процесс обновляет его раз в несколько секунд, а healthcheck смотрит, не устарел ли он.
Зависший процесс перестаёт обновлять файл — Docker помечает контейнер unhealthy и перезапускает.

Запуск проверки: python -m bot.core.heartbeat
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import tempfile
import time
from pathlib import Path

HEARTBEAT_FILE = Path(os.environ.get("HEARTBEAT_FILE", Path(tempfile.gettempdir()) / "bot-heartbeat"))
MAX_AGE_SECONDS = 90


def beat() -> None:
    with contextlib.suppress(OSError):
        HEARTBEAT_FILE.touch()


async def run_heartbeat(every_seconds: float = 20) -> None:
    """Фоновая задача: пульс, пока цикл событий жив."""
    while True:
        beat()
        await asyncio.sleep(every_seconds)


def is_alive() -> bool:
    try:
        return time.time() - HEARTBEAT_FILE.stat().st_mtime < MAX_AGE_SECONDS
    except OSError:
        return False


if __name__ == "__main__":
    sys.exit(0 if is_alive() else 1)
