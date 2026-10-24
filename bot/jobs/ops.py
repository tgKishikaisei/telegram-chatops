"""Фоновые задачи: публикация/обновление постов инцидентов (outbox), уведомления, эскалация,
снятие «заглушки», истечение запросов на действия, уборка старых доставок вебхуков."""

from __future__ import annotations

import contextlib
import logging
from datetime import timedelta
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bot.config import Settings
from bot.core.clock import utcnow
from bot.gateways import team
from bot.models import Role
from bot.repositories import ops as repo
from bot.services import incidents

logger = logging.getLogger(__name__)


async def sync_posts(bot: Bot, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    async with session_factory() as session:
        pending = await repo.to_sync(session)
        if not pending:
            return
        names = {member.user_id: member.name for member in await repo.members_with(session, list(Role))}
    for incident in pending:
        try:
            message_id = await team.publish(bot, settings.team_chat_id, incident, settings.tz, names)
        except TelegramAPIError:
            logger.warning("Не удалось опубликовать инцидент", extra={"incident_id": incident.id})
            continue  # попробуем на следующем проходе — флаг не снят
        async with session_factory() as session:
            await repo.synced(session, incident.id, incident.revision, message_id)
            await session.commit()


async def send_notices(bot: Bot, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    async with session_factory() as session:
        notices = await repo.unsent_notices(session)
    for notice in notices:
        try:
            await bot.send_message(settings.team_chat_id, escape(notice.text), disable_web_page_preview=True)
        except TelegramAPIError:
            return
        async with session_factory() as session:
            await repo.mark_notice_sent(session, notice.id)
            await session.commit()


async def escalate(bot: Bot, session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    now = utcnow()
    async with session_factory() as session:
        claimed = await incidents.due_escalations(session, now, settings.escalation_minutes)
        await session.commit()
        if not claimed:
            return
        oncall = await incidents.current_oncall(session, now, settings.tz)
        operators = [member.user_id for member in await repo.members_with(session, [Role.OPERATOR, Role.ADMIN])]
    for incident, level in claimed:
        text = (f"🚨 Инцидент #{incident.id} «{escape(incident.title)}» не взят в работу "
                f"{settings.escalation_minutes * level} мин.")
        recipients = [oncall] if level == 1 and oncall else operators
        for user_id in recipients:
            with contextlib.suppress(TelegramAPIError):
                await bot.send_message(user_id, text + " Откройте чат команды и нажмите «✋ Беру».")
        if level == 2:
            with contextlib.suppress(TelegramAPIError):
                await bot.send_message(settings.team_chat_id, text + " Эскалация на всех операторов.")


async def housekeeping(session_factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    now = utcnow()
    async with session_factory() as session:
        await repo.silence_expired(session, now)
        for request_id in await repo.expire_requests(session, now - timedelta(minutes=settings.approval_minutes),
                                                     now):
            await repo.audit(session, None, "action.expired", f"#{request_id}", now)
        await repo.drop_old_deliveries(session, now - timedelta(days=7))
        await session.commit()
