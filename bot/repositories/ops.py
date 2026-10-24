"""Участники, инциденты, доставки вебхуков, уведомления, дежурства, действия, аудит."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import case, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot.core.database import insert_or_ignore
from bot.models import (
    ActionRequest,
    ActionStatus,
    AuditEntry,
    Incident,
    IncidentStatus,
    Member,
    Notice,
    OnCall,
    OnCallOverride,
    Role,
    Severity,
    WebhookDelivery,
)

ACTIVE = (IncidentStatus.OPEN, IncidentStatus.ACKED, IncidentStatus.SILENCED)
SEVERITY_ORDER = case({Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.INFO: 2}, value=Incident.severity, else_=3)

# ---------- участники ----------

async def get_member(session: AsyncSession, user_id: int) -> Member | None:
    return await session.get(Member, user_id, populate_existing=True)


async def ensure_member(session: AsyncSession, user_id: int, name: str, role: Role, now: datetime) -> None:
    await insert_or_ignore(session, Member, {"user_id": user_id, "name": name[:64], "role": role, "created_at": now},
                           ["user_id"])


async def rename(session: AsyncSession, user_id: int, name: str) -> None:
    await session.execute(update(Member).where(Member.user_id == user_id).values(name=name[:64]))


async def set_role(session: AsyncSession, user_id: int, role: Role) -> bool:
    result = await session.execute(update(Member).where(Member.user_id == user_id).values(role=role))
    return result.rowcount == 1


async def members_with(session: AsyncSession, roles: Iterable[Role]) -> list[Member]:
    return list(await session.scalars(select(Member).where(Member.role.in_(list(roles))).order_by(Member.user_id)))


# ---------- инциденты ----------

async def active_by_fingerprint(session: AsyncSession, fingerprint: str) -> Incident | None:
    return await session.scalar(select(Incident).where(Incident.fingerprint == fingerprint,
                                                       Incident.status.in_(ACTIVE))
                                .order_by(Incident.id.desc()).limit(1).execution_options(populate_existing=True))


async def add_incident(session: AsyncSession, incident: Incident) -> Incident:
    session.add(incident)
    await session.flush()
    return incident


async def get_incident(session: AsyncSession, incident_id: int) -> Incident | None:
    return await session.get(Incident, incident_id, populate_existing=True)


async def change(session: AsyncSession, incident_id: int, allowed_from: Iterable[IncidentStatus],
                 **values: Any) -> bool:
    """Любое изменение инцидента помечает пост к обновлению и увеличивает ревизию."""
    result = await session.execute(update(Incident).where(Incident.id == incident_id,
                                                          Incident.status.in_(list(allowed_from)))
                                   .values(needs_sync=True, revision=Incident.revision + 1, **values)
                                   .execution_options(synchronize_session=False))
    return result.rowcount == 1


async def repeat_event(session: AsyncSession, incident_id: int, now: datetime) -> None:
    await session.execute(update(Incident).where(Incident.id == incident_id)
                          .values(events=Incident.events + 1, last_event_at=now, needs_sync=True,
                                  revision=Incident.revision + 1).execution_options(synchronize_session=False))


async def to_sync(session: AsyncSession, limit: int = 20) -> list[Incident]:
    return list(await session.scalars(select(Incident).where(Incident.needs_sync.is_(True)).order_by(Incident.id)
                                      .limit(limit).execution_options(populate_existing=True)))


async def synced(session: AsyncSession, incident_id: int, revision: int, message_id: int | None) -> None:
    """Флаг снимается, только если после чтения инцидент не менялся (иначе пост обновим ещё раз)."""
    values: dict[str, Any] = {"needs_sync": False}
    if message_id is not None:
        values["message_id"] = message_id
    await session.execute(update(Incident).where(Incident.id == incident_id, Incident.revision == revision)
                          .values(**values))
    if message_id is not None:  # message_id сохраняем в любом случае — иначе пост продублируется
        await session.execute(update(Incident).where(Incident.id == incident_id, Incident.message_id.is_(None))
                              .values(message_id=message_id))


async def active_incidents(session: AsyncSession, limit: int = 30) -> list[Incident]:
    return list(await session.scalars(select(Incident).where(Incident.status.in_(ACTIVE))
                                      .order_by(SEVERITY_ORDER, Incident.id).limit(limit)))


async def escalation_candidates(session: AsyncSession) -> list[Incident]:
    return list(await session.scalars(select(Incident).where(Incident.status == IncidentStatus.OPEN,
                                                             Incident.severity == Severity.CRITICAL,
                                                             Incident.escalation < 2)))


async def claim_escalation(session: AsyncSession, incident_id: int, level: int) -> bool:
    result = await session.execute(update(Incident).where(Incident.id == incident_id,
                                                          Incident.status == IncidentStatus.OPEN,
                                                          Incident.escalation == level - 1)
                                   .values(escalation=level))
    return result.rowcount == 1


async def silence_expired(session: AsyncSession, now: datetime) -> list[int]:
    ids = list(await session.scalars(select(Incident.id).where(Incident.status == IncidentStatus.SILENCED,
                                                               Incident.silenced_until < now)))
    return [incident_id for incident_id in ids
            if await change(session, incident_id, [IncidentStatus.SILENCED], status=IncidentStatus.OPEN,
                            silenced_until=None, escalation=0)]


# ---------- вебхуки и уведомления ----------

async def record_delivery(session: AsyncSession, source: str, delivery_id: str, now: datetime) -> bool:
    return await insert_or_ignore(session, WebhookDelivery, {"source": source, "delivery_id": delivery_id[:128],
                                                             "received_at": now}, ["source", "delivery_id"])


async def drop_old_deliveries(session: AsyncSession, before: datetime) -> None:
    await session.execute(delete(WebhookDelivery).where(WebhookDelivery.received_at < before))


async def add_notice(session: AsyncSession, text: str, now: datetime) -> None:
    session.add(Notice(text=text[:3500], created_at=now))
    await session.flush()


async def unsent_notices(session: AsyncSession, limit: int = 20) -> list[Notice]:
    return list(await session.scalars(select(Notice).where(Notice.sent.is_(False)).order_by(Notice.id).limit(limit)))


async def mark_notice_sent(session: AsyncSession, notice_id: int) -> None:
    await session.execute(update(Notice).where(Notice.id == notice_id).values(sent=True))


# ---------- дежурства ----------

async def set_rotation(session: AsyncSession, user_ids: list[int]) -> None:
    await session.execute(delete(OnCall))
    for position, user_id in enumerate(user_ids):
        session.add(OnCall(position=position, user_id=user_id))
    await session.flush()


async def rotation(session: AsyncSession) -> list[int]:
    return list(await session.scalars(select(OnCall.user_id).order_by(OnCall.position)))


async def add_override(session: AsyncSession, user_id: int, until: datetime, now: datetime) -> None:
    session.add(OnCallOverride(user_id=user_id, until=until, created_at=now))
    await session.flush()


async def active_override(session: AsyncSession, now: datetime) -> int | None:
    return await session.scalar(select(OnCallOverride.user_id).where(OnCallOverride.until > now)
                                .order_by(OnCallOverride.id.desc()).limit(1))


# ---------- действия и аудит ----------

async def add_request(session: AsyncSession, request: ActionRequest) -> ActionRequest:
    session.add(request)
    await session.flush()
    return request


async def get_request(session: AsyncSession, request_id: int) -> ActionRequest | None:
    return await session.get(ActionRequest, request_id, populate_existing=True)


async def move_request(session: AsyncSession, request_id: int, allowed_from: Iterable[ActionStatus],
                       **values: Any) -> bool:
    result = await session.execute(update(ActionRequest).where(ActionRequest.id == request_id,
                                                               ActionRequest.status.in_(list(allowed_from)))
                                   .values(**values).execution_options(synchronize_session=False))
    return result.rowcount == 1


async def expire_requests(session: AsyncSession, created_before: datetime, now: datetime) -> list[int]:
    ids = list(await session.scalars(select(ActionRequest.id).where(ActionRequest.status == ActionStatus.PENDING,
                                                                    ActionRequest.created_at < created_before)))
    return [request_id for request_id in ids
            if await move_request(session, request_id, [ActionStatus.PENDING], status=ActionStatus.EXPIRED,
                                  decided_at=now)]


async def audit(session: AsyncSession, actor_id: int | None, event: str, details: str, now: datetime) -> None:
    session.add(AuditEntry(actor_id=actor_id, event=event[:48], details=details[:2000], created_at=now))
    await session.flush()


async def recent_audit(session: AsyncSession, limit: int = 20) -> list[AuditEntry]:
    return list(await session.scalars(select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit)))
