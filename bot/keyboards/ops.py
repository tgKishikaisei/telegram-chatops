from __future__ import annotations

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.models import Incident, IncidentStatus


class IncCb(CallbackData, prefix="inc"):
    incident_id: int
    action: str   # ack | resolve | silence


class ActCb(CallbackData, prefix="act"):
    request_id: int
    approve: int


def incident_keyboard(incident: Incident) -> InlineKeyboardMarkup | None:
    if incident.status is IncidentStatus.RESOLVED:
        return None
    builder = InlineKeyboardBuilder()
    if incident.status in (IncidentStatus.OPEN, IncidentStatus.SILENCED):
        builder.button(text="✋ Беру", callback_data=IncCb(incident_id=incident.id, action="ack"))
    builder.button(text="✅ Решено", callback_data=IncCb(incident_id=incident.id, action="resolve"))
    if incident.status in (IncidentStatus.OPEN, IncidentStatus.ACKED):
        builder.button(text="🔕 Заглушить 1 ч", callback_data=IncCb(incident_id=incident.id, action="silence"))
    builder.adjust(2, 1)
    return builder.as_markup()


def approval_keyboard(request_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтверждаю", callback_data=ActCb(request_id=request_id, approve=1))
    builder.button(text="✖️ Отклонить", callback_data=ActCb(request_id=request_id, approve=0))
    return builder.as_markup()
