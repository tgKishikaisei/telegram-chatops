"""Пост инцидента в чате команды: создаётся один раз, дальше только редактируется."""

from __future__ import annotations

import contextlib
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest

from bot.core.clock import to_local
from bot.keyboards.ops import incident_keyboard
from bot.models import Incident, IncidentStatus, Severity

ICON = {Severity.CRITICAL: "🔴", Severity.WARNING: "🟠", Severity.INFO: "🔵"}
STATUS = {IncidentStatus.OPEN: "открыт", IncidentStatus.ACKED: "в работе", IncidentStatus.RESOLVED: "решён",
          IncidentStatus.SILENCED: "заглушен"}


def render(incident: Incident, tz: ZoneInfo, names: dict[int, str]) -> str:
    def who(user_id: int | None) -> str:
        return escape(names.get(user_id, str(user_id))) if user_id else "система"

    icon = "✅" if incident.status is IncidentStatus.RESOLVED else ICON[incident.severity]
    lines = [f"{icon} <b>#{incident.id} {escape(incident.title)}</b>",
             f"{incident.severity.value} · {STATUS[incident.status]} · с {to_local(incident.opened_at, tz):%d.%m %H:%M}"
             + (f" · повторов: {incident.events}" if incident.events > 1 else "")]
    if incident.details:
        lines.append(escape(incident.details[:800]))
    if incident.acked_by:
        lines.append(f"✋ Взял: {who(incident.acked_by)}")
    if incident.status is IncidentStatus.SILENCED and incident.silenced_until:
        lines.append(f"🔕 Заглушен до {to_local(incident.silenced_until, tz):%H:%M}")
    if incident.status is IncidentStatus.RESOLVED:
        lines.append(f"✅ Решил: {who(incident.resolved_by)}")
    return "\n".join(lines)[:4000]


async def publish(bot: Bot, chat_id: int, incident: Incident, tz: ZoneInfo, names: dict[int, str]) -> int:
    """Отправляет или обновляет пост. Возвращает message_id."""
    text, markup = render(incident, tz, names), incident_keyboard(incident)
    if incident.message_id is not None:
        with contextlib.suppress(TelegramBadRequest):  # «message is not modified»
            await bot.edit_message_text(text, chat_id=chat_id, message_id=incident.message_id, reply_markup=markup)
        return incident.message_id
    message = await bot.send_message(chat_id, text, reply_markup=markup)
    return message.message_id
