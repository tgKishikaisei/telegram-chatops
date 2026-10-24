"""Входящие вебхуки: проверка подлинности и разбор в единые события. Без БД и сети — чистые функции.

- Alertmanager / Grafana: заголовок Authorization: Bearer <секрет> (оба умеют задавать его в настройках),
  сравнение за постоянное время. Формат — стандартный {"alerts": [...]}, а также простой
  {"title", "severity", "text", "id"} для своих скриптов.
- GitHub: подпись X-Hub-Signature-256 (HMAC-SHA256 тела секретом вебхука), повтор доставки
  отсекается по X-GitHub-Delivery.
Все строки из вебхуков считаются недоверенными: обрезаются, управляющие символы удаляются,
при показе — экранируются.
"""

from __future__ import annotations

import hashlib
import hmac
import unicodedata
from dataclasses import dataclass
from typing import Any

from bot.models import Severity


@dataclass(frozen=True)
class AlertEvent:
    fingerprint: str
    firing: bool                 # False — алерт разрешился
    title: str
    severity: Severity
    details: str


@dataclass(frozen=True)
class GithubEvent:
    alert: AlertEvent | None = None   # сбой сборки/деплоя (или его исправление)
    notice: str | None = None         # просто уведомление в чат


def clean(value: Any, limit: int) -> str:
    text = "".join(ch for ch in str(value or "") if ch == "\n" or unicodedata.category(ch)[0] != "C")
    return text.strip()[:limit]


def check_bearer(header: str | None, secret: str) -> bool:
    scheme, _, token = (header or "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(token.strip().encode(), secret.encode())


def verify_github(body: bytes, signature: str | None, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest((signature or "").encode(), expected.encode())


def _severity(value: Any) -> Severity:
    text = str(value or "").lower()
    if text in ("critical", "page", "high", "error", "fatal"):
        return Severity.CRITICAL
    if text in ("warning", "warn", "medium"):
        return Severity.WARNING
    return Severity.INFO


def parse_alerts(payload: Any) -> list[AlertEvent]:
    if not isinstance(payload, dict):
        return []
    events = []
    if isinstance(payload.get("alerts"), list):
        for alert in payload["alerts"][:50]:
            if not isinstance(alert, dict):
                continue
            labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
            annotations = alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {}
            name = clean(labels.get("alertname") or alert.get("title") or "Алерт", 120)
            where = clean(labels.get("instance") or labels.get("service") or labels.get("job") or "", 60)
            fingerprint = clean(alert.get("fingerprint") or f"{name}:{where}", 100)
            details = clean(annotations.get("description") or annotations.get("summary") or "", 1500)
            events.append(AlertEvent(f"alert:{fingerprint}", alert.get("status") != "resolved",
                                     f"{name}" + (f" — {where}" if where else ""),
                                     _severity(labels.get("severity")), details))
        return events
    if payload.get("title"):
        identifier = clean(payload.get("id") or payload.get("title"), 100)
        events.append(AlertEvent(f"custom:{identifier}", payload.get("status") != "resolved",
                                 clean(payload["title"], 160), _severity(payload.get("severity")),
                                 clean(payload.get("text"), 1500)))
    return events


def parse_github(event: str, payload: Any) -> GithubEvent:
    if not isinstance(payload, dict):
        return GithubEvent()
    repo = clean((payload.get("repository") or {}).get("full_name"), 100)
    if event == "workflow_run" and payload.get("action") == "completed":
        run = payload.get("workflow_run") or {}
        branch, name = clean(run.get("head_branch"), 80), clean(run.get("name"), 80)
        conclusion, url = clean(run.get("conclusion"), 20), clean(run.get("html_url"), 300)
        default = clean((payload.get("repository") or {}).get("default_branch") or "main", 80)
        if branch != default:
            return GithubEvent()
        fingerprint = f"gh:wf:{repo}:{name}:{branch}"
        if conclusion == "failure":
            return GithubEvent(alert=AlertEvent(fingerprint, True, f"Сборка «{name}» упала в {repo}@{branch}",
                                                Severity.WARNING, url))
        if conclusion == "success":
            return GithubEvent(alert=AlertEvent(fingerprint, False, f"Сборка «{name}» снова зелёная", Severity.INFO,
                                                url))
        return GithubEvent()
    if event == "deployment_status":
        status = payload.get("deployment_status") or {}
        state = clean(status.get("state"), 20)
        environment = clean(status.get("environment") or (payload.get("deployment") or {}).get("environment"), 60)
        fingerprint = f"gh:deploy:{repo}:{environment}"
        if state in ("failure", "error"):
            return GithubEvent(alert=AlertEvent(fingerprint, True, f"Деплой {repo} → {environment} не удался",
                                                Severity.CRITICAL, clean(status.get("description"), 500)))
        if state == "success":
            return GithubEvent(alert=AlertEvent(fingerprint, False, f"Деплой {repo} → {environment}", Severity.INFO,
                                                ""), notice=f"🚀 Задеплоено: {repo} → {environment}")
        return GithubEvent()
    if event == "pull_request":
        pull = payload.get("pull_request") or {}
        title, number = clean(pull.get("title"), 120), payload.get("number")
        user = clean((pull.get("user") or {}).get("login"), 40)
        if payload.get("action") == "opened":
            return GithubEvent(notice=f"🔀 PR #{number} в {repo}: {title} ({user})")
        if payload.get("action") == "closed" and pull.get("merged"):
            return GithubEvent(notice=f"✅ Влит PR #{number} в {repo}: {title}")
    return GithubEvent()
