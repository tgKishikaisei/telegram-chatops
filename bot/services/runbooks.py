"""Runbook-действия: только заранее описанные в bot/content/actions.json, параметры — по шаблонам.

Бот ничего не выполняет у себя (никакого shell): он отправляет подписанный запрос агенту в инфраструктуре.
Опасные действия — правило «двух ключей»: запросил один оператор, подтвердил ДРУГОЙ, и только в течение
срока жизни запроса. Каждый шаг пишется в журнал аудита.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from bot.models import ActionRequest, ActionStatus, Member, Role
from bot.repositories import ops as repo
from bot.services.incidents import OpsError, require

ACTIONS_FILE = Path(__file__).resolve().parent.parent / "content" / "actions.json"


class ActionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,40}$")
    title: str = Field(min_length=3, max_length=80)
    dangerous: bool = True
    params: dict[str, str] = Field(default_factory=dict)   # имя → регулярное выражение значения

    @field_validator("params")
    @classmethod
    def _patterns(cls, value: dict[str, str]) -> dict[str, str]:
        for key, pattern in value.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", key):
                raise ValueError(f"Недопустимое имя параметра: {key}")
            try:
                re.compile(pattern)
            except re.error as exc:  # не ValueError — иначе pydantic не превратит её в ошибку проверки
                raise ValueError(f"Некорректный шаблон параметра {key}") from exc
        return value


class Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_url: str = Field(pattern=r"^https?://[^\s]+$")
    actions: list[ActionSpec]

    def get(self, name: str) -> ActionSpec | None:
        return next((action for action in self.actions if action.name == name), None)


def load_catalog(path: Path = ACTIONS_FILE) -> Catalog:
    return Catalog.model_validate(json.loads(path.read_text(encoding="utf-8")))


def parse_params(spec: ActionSpec, args: list[str]) -> dict[str, str]:
    """`count=3 service=api` → словарь; каждое значение — по шаблону из каталога, лишние ключи запрещены."""
    params: dict[str, str] = {}
    for arg in args:
        key, sep, value = arg.partition("=")
        if not sep or key not in spec.params:
            raise OpsError(f"Неизвестный параметр «{key}». Нужны: {', '.join(spec.params) or 'без параметров'}")
        if not re.fullmatch(spec.params[key], value):
            raise OpsError(f"Недопустимое значение параметра {key}")
        params[key] = value
    missing = set(spec.params) - set(params)
    if missing:
        raise OpsError(f"Не хватает параметров: {', '.join(sorted(missing))}")
    return params


@dataclass(frozen=True)
class Requested:
    request: ActionRequest
    needs_approval: bool


async def request(session: AsyncSession, catalog: Catalog, member: Member | None, name: str, args: list[str],
                  now: datetime) -> Requested:
    require(member, Role.OPERATOR)
    spec = catalog.get(name)
    if spec is None:
        raise OpsError("Нет такого действия — список: /actions")
    params = parse_params(spec, args)
    status = ActionStatus.PENDING if spec.dangerous else ActionStatus.RUNNING
    created = await repo.add_request(session, ActionRequest(action=spec.name, params=params,
                                                            requested_by=member.user_id, status=status,
                                                            created_at=now))
    await repo.audit(session, member.user_id, "action.requested", f"#{created.id} {spec.name} {params}", now)
    return Requested(created, spec.dangerous)


async def decide(session: AsyncSession, member: Member | None, request_id: int, approve: bool, now: datetime,
                 window_minutes: int) -> ActionRequest:
    require(member, Role.OPERATOR)
    action = await repo.get_request(session, request_id)
    if action is None or action.status is not ActionStatus.PENDING:
        raise OpsError("Запрос уже решён или истёк")
    if approve and action.requested_by == member.user_id:
        raise OpsError("Подтвердить должен другой человек (правило двух ключей)")
    if now - action.created_at > timedelta(minutes=window_minutes):
        await repo.move_request(session, request_id, [ActionStatus.PENDING], status=ActionStatus.EXPIRED,
                                decided_at=now)
        raise OpsError("Запрос истёк — запросите действие заново")
    target = ActionStatus.RUNNING if approve else ActionStatus.REJECTED
    if not await repo.move_request(session, request_id, [ActionStatus.PENDING], status=target,
                                   approved_by=member.user_id, decided_at=now):
        raise OpsError("Запрос уже решён")
    await repo.audit(session, member.user_id, "action.approved" if approve else "action.rejected", f"#{request_id}",
                     now)
    return await repo.get_request(session, request_id)


async def finish(session: AsyncSession, request_id: int, ok: bool, output: str, now: datetime) -> ActionRequest:
    await repo.move_request(session, request_id, [ActionStatus.RUNNING],
                            status=ActionStatus.DONE if ok else ActionStatus.FAILED, result=output[:3000])
    await repo.audit(session, None, "action.done" if ok else "action.failed", f"#{request_id}", now)
    return await repo.get_request(session, request_id)
