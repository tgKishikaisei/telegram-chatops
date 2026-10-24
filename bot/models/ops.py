from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Enum, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from bot.core.clock import utcnow
from bot.core.database import Base


def _enum(kind: type[enum.Enum]) -> Enum:
    return Enum(kind, native_enum=False, length=20, values_callable=lambda members: [m.value for m in members])


class Role(enum.StrEnum):
    VIEWER = "viewer"      # видит инциденты и статус
    OPERATOR = "operator"  # берёт и закрывает инциденты, запрашивает действия, подтверждает чужие
    ADMIN = "admin"        # роли и дежурства


class Severity(enum.StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"


class IncidentStatus(enum.StrEnum):
    OPEN = "open"
    ACKED = "acked"
    RESOLVED = "resolved"
    SILENCED = "silenced"


class ActionStatus(enum.StrEnum):
    PENDING = "pending"      # опасное действие ждёт второго человека
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class Member(Base):
    __tablename__ = "members"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    name: Mapped[str] = mapped_column(String(64))
    role: Mapped[Role] = mapped_column(_enum(Role), default=Role.VIEWER)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Incident(Base):
    """Инцидент. Пост в чате команды обновляет фоновая задача по флагу needs_sync (outbox):
    вебхук отвечает сразу, а сбой Telegram не теряет алерт."""

    __tablename__ = "incidents"
    __table_args__ = (Index("ix_incidents_fp_status", "fingerprint", "status"),
                      Index("ix_incidents_sync", "needs_sync"))

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))           # alert | github | manual
    fingerprint: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(200))
    details: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[Severity] = mapped_column(_enum(Severity))
    status: Mapped[IncidentStatus] = mapped_column(_enum(IncidentStatus), default=IncidentStatus.OPEN)
    events: Mapped[int] = mapped_column(default=1)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    needs_sync: Mapped[bool] = mapped_column(default=True)
    revision: Mapped[int] = mapped_column(default=1)          # растёт при каждом изменении — пост не «откатится»
    escalation: Mapped[int] = mapped_column(default=0)        # 0 — нет, 1 — дежурному, 2 — всем операторам
    opened_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_event_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    acked_by: Mapped[int | None] = mapped_column(BigInteger)
    acked_at: Mapped[datetime | None] = mapped_column(DateTime)
    resolved_by: Mapped[int | None] = mapped_column(BigInteger)  # None при авторазрешении алертом
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    silenced_until: Mapped[datetime | None] = mapped_column(DateTime)


class WebhookDelivery(Base):
    """Защита от повтора: один и тот же вебхук (id доставки) обрабатывается один раз."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("source", "delivery_id", name="uq_webhook_delivery"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(16))
    delivery_id: Mapped[str] = mapped_column(String(128))
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Notice(Base):
    """Сообщение в чат команды (не инцидент): деплой, PR, успешная сборка. Тоже через outbox."""

    __tablename__ = "notices"

    id: Mapped[int] = mapped_column(primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    sent: Mapped[bool] = mapped_column(default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OnCall(Base):
    """Порядок дежурств: позиция → человек. Текущий дежурный — по номеру недели; можно «взять смену»."""

    __tablename__ = "oncall"

    position: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    user_id: Mapped[int] = mapped_column(BigInteger)


class OnCallOverride(Base):
    __tablename__ = "oncall_overrides"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    until: Mapped[datetime] = mapped_column(DateTime, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ActionRequest(Base):
    __tablename__ = "action_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    action: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    requested_by: Mapped[int] = mapped_column(BigInteger)
    approved_by: Mapped[int | None] = mapped_column(BigInteger)
    status: Mapped[ActionStatus] = mapped_column(_enum(ActionStatus))
    result: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditEntry(Base):
    """Журнал аудита: кто, что и когда сделал. Только добавление."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, index=True)  # None — система / вебхук
    event: Mapped[str] = mapped_column(String(48))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
