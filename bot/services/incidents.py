"""Инциденты: приём алертов (с дедупликацией по отпечатку), действия команды, эскалация, дежурства.

Права: viewer видит; operator берёт, закрывает, глушит; admin — роли и порядок дежурств.
Каждое изменение — атомарный переход статуса и запись в журнал аудита.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.clock import to_local
from bot.models import Incident, IncidentStatus, Member, Role, Severity
from bot.repositories import ops as repo
from bot.services.webhooks import AlertEvent

RANK = {Role.VIEWER: 0, Role.OPERATOR: 1, Role.ADMIN: 2}
SILENCE = timedelta(hours=1)
ROTATION_ANCHOR = date(2024, 1, 1)  # понедельник: смена дежурного — по понедельникам


class OpsError(Exception):
    """Текст показывается пользователю."""


@dataclass(frozen=True)
class Ingested:
    incident: Incident | None
    outcome: str   # created | repeated | resolved | ignored


def require(member: Member | None, role: Role) -> Member:
    if member is None or RANK[member.role] < RANK[role]:
        raise OpsError("Недостаточно прав" if member else "Вы не в команде — попросите администратора добавить вас")
    return member


async def ingest(session: AsyncSession, event: AlertEvent, source: str, now: datetime) -> Ingested:
    active = await repo.active_by_fingerprint(session, event.fingerprint)
    if not event.firing:
        if active is None:
            return Ingested(None, "ignored")
        await repo.change(session, active.id, repo.ACTIVE, status=IncidentStatus.RESOLVED, resolved_at=now,
                          resolved_by=None)
        await repo.audit(session, None, "incident.auto_resolved", f"#{active.id} {event.title}", now)
        return Ingested(await repo.get_incident(session, active.id), "resolved")
    if active is not None:
        await repo.repeat_event(session, active.id, now)
        return Ingested(await repo.get_incident(session, active.id), "repeated")
    incident = await repo.add_incident(session, Incident(
        source=source, fingerprint=event.fingerprint, title=event.title, details=event.details,
        severity=event.severity, status=IncidentStatus.OPEN, opened_at=now, last_event_at=now))
    await repo.audit(session, None, "incident.opened", f"#{incident.id} [{event.severity}] {event.title}", now)
    return Ingested(incident, "created")


async def open_manual(session: AsyncSession, member: Member | None, severity: str, title: str,
                      now: datetime) -> Incident:
    require(member, Role.OPERATOR)
    title = "".join(ch for ch in (title or "") if unicodedata.category(ch)[0] != "C").strip()[:160]
    if len(title) < 3:
        raise OpsError("Формат: /incident critical|warning|info Описание проблемы")
    try:
        level = Severity(severity)
    except ValueError as exc:
        raise OpsError("Важность: critical, warning или info") from exc
    incident = await repo.add_incident(session, Incident(
        source="manual", fingerprint=f"manual:{now.timestamp()}:{member.user_id}", title=title, details="",
        severity=level, status=IncidentStatus.OPEN, opened_at=now, last_event_at=now))
    await repo.audit(session, member.user_id, "incident.opened", f"#{incident.id} [{level}] {title}", now)
    return incident


TRANSITIONS = {
    "ack": ([IncidentStatus.OPEN, IncidentStatus.SILENCED], IncidentStatus.ACKED),
    "resolve": (list(repo.ACTIVE), IncidentStatus.RESOLVED),
    "silence": ([IncidentStatus.OPEN, IncidentStatus.ACKED], IncidentStatus.SILENCED),
}


async def act(session: AsyncSession, member: Member | None, incident_id: int, action: str,
              now: datetime) -> Incident:
    require(member, Role.OPERATOR)
    if action not in TRANSITIONS:
        raise OpsError("Неизвестное действие")
    allowed, target = TRANSITIONS[action]
    values: dict = {"status": target}
    if action == "ack":
        values.update(acked_by=member.user_id, acked_at=now, silenced_until=None)
    elif action == "resolve":
        values.update(resolved_by=member.user_id, resolved_at=now)
    else:
        values.update(silenced_until=now + SILENCE)
    if not await repo.change(session, incident_id, allowed, **values):
        raise OpsError("Статус инцидента уже изменился")
    await repo.audit(session, member.user_id, f"incident.{action}", f"#{incident_id}", now)
    return await repo.get_incident(session, incident_id)


async def due_escalations(session: AsyncSession, now: datetime, minutes: int) -> list[tuple[Incident, int]]:
    """critical без реакции: через N минут — дежурному, через 2N — всем операторам. Каждый уровень — один раз."""
    claimed = []
    for incident in await repo.escalation_candidates(session):
        level = incident.escalation + 1
        if now - incident.opened_at >= timedelta(minutes=minutes * level) and \
                await repo.claim_escalation(session, incident.id, level):
            await repo.audit(session, None, "incident.escalated", f"#{incident.id} level {level}", now)
            claimed.append((incident, level))
    return claimed


# ---------- дежурства ----------

async def current_oncall(session: AsyncSession, now: datetime, tz: ZoneInfo) -> int | None:
    override = await repo.active_override(session, now)
    if override is not None:
        return override
    order = await repo.rotation(session)
    if not order:
        return None
    week = (to_local(now, tz).date().toordinal() - ROTATION_ANCHOR.toordinal()) // 7
    return order[week % len(order)]


async def set_rotation(session: AsyncSession, member: Member | None, user_ids: list[int], now: datetime) -> None:
    require(member, Role.ADMIN)
    if not user_ids or len(user_ids) != len(set(user_ids)):
        raise OpsError("Укажите ID дежурных через пробел, без повторов")
    for user_id in user_ids:
        candidate = await repo.get_member(session, user_id)
        if candidate is None or RANK[candidate.role] < RANK[Role.OPERATOR]:
            raise OpsError(f"{user_id} — не оператор команды")
    await repo.set_rotation(session, user_ids)
    await repo.audit(session, member.user_id, "oncall.rotation", " ".join(map(str, user_ids)), now)


async def take_shift(session: AsyncSession, member: Member | None, hours: int, now: datetime) -> datetime:
    require(member, Role.OPERATOR)
    if not 1 <= hours <= 168:
        raise OpsError("Смена — от 1 до 168 часов")
    until = now + timedelta(hours=hours)
    await repo.add_override(session, member.user_id, until, now)
    await repo.audit(session, member.user_id, "oncall.take", f"до {until:%Y-%m-%d %H:%M} UTC", now)
    return until


async def set_role(session: AsyncSession, member: Member | None, user_id: int, role_name: str, name: str,
                   now: datetime) -> None:
    require(member, Role.ADMIN)
    try:
        role = Role(role_name)
    except ValueError as exc:
        raise OpsError("Роль: viewer, operator или admin") from exc
    if user_id == member.user_id and role is not Role.ADMIN:
        raise OpsError("Нельзя понизить самого себя — попросите другого администратора")
    await repo.ensure_member(session, user_id, name, role, now)
    await repo.set_role(session, user_id, role)
    await repo.audit(session, member.user_id, "member.role", f"{user_id} → {role}", now)
