"""Единая обработка ошибок бота.

Пользователь видит короткое сообщение и код обращения (request_id), без технических
подробностей. Полный стек — только в логе, по тому же коду его легко найти.
"""

from __future__ import annotations

import logging

from aiogram import Dispatcher
from aiogram.types import ErrorEvent

from bot.core.logging import request_id

logger = logging.getLogger(__name__)


class UserError(Exception):
    """Ошибка, текст которой можно показать пользователю как есть (неверный ввод, нет прав…)."""


def register_error_handler(dispatcher: Dispatcher) -> None:
    @dispatcher.errors()
    async def on_error(event: ErrorEvent) -> bool:
        update = event.update
        if isinstance(event.exception, UserError):
            text = str(event.exception)
        else:
            logger.exception("Ошибка при обработке апдейта", exc_info=event.exception)
            text = f"Что-то пошло не так. Попробуйте ещё раз; если повторится — сообщите код {request_id.get() or '—'}."

        try:
            if update.callback_query:
                await update.callback_query.answer(text[:200], show_alert=True)
            elif update.message:
                await update.message.answer(text)
        except Exception:
            logger.warning("Не удалось сообщить пользователю об ошибке")
        return True
