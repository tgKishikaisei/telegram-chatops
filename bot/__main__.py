"""Точка входа. Запуск из папки бота: python -m bot"""

import asyncio
import contextlib

from bot.app import main

if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
