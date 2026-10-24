"""ChatOps: вебхуки, инциденты, роли, эскалация, дежурства, runbook-действия, агент."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest

from bot.core.clock import utcnow
from bot.gateways.agent import AgentClient, sign
from bot.models import ActionStatus, IncidentStatus, Role, Severity
from bot.repositories import ops as repo
from bot.services import incidents, runbooks, webhooks
from bot.services.incidents import OpsError
from bot.services.runbooks import Catalog
from bot.services.webhooks import AlertEvent

TZ = ZoneInfo("UTC")
ALERTMANAGER = {"alerts": [{"status": "firing", "fingerprint": "abc123",
                            "labels": {"alertname": "HighErrorRate", "severity": "critical", "instance": "api-1"},
                            "annotations": {"description": "5xx > 5% <b>за 5 минут</b>"}}]}


def test_bearer_and_github_signature():
    assert webhooks.check_bearer("Bearer s3cret", "s3cret")
    assert not webhooks.check_bearer("Bearer wrong", "s3cret")
    assert not webhooks.check_bearer("s3cret", "s3cret")
    assert not webhooks.check_bearer(None, "s3cret")
    body = b'{"zen": "ok"}'
    good = "sha256=" + hmac.new(b"gh-secret", body, hashlib.sha256).hexdigest()
    assert webhooks.verify_github(body, good, "gh-secret")
    assert not webhooks.verify_github(body + b" ", good, "gh-secret")
    assert not webhooks.verify_github(body, None, "gh-secret")


def test_parse_alerts():
    [event] = webhooks.parse_alerts(ALERTMANAGER)
    assert event.firing and event.severity is Severity.CRITICAL and event.title == "HighErrorRate — api-1"
    assert event.fingerprint == "alert:abc123"
    resolved = json.loads(json.dumps(ALERTMANAGER))
    resolved["alerts"][0]["status"] = "resolved"
    assert not webhooks.parse_alerts(resolved)[0].firing
    [custom] = webhooks.parse_alerts({"title": "Backup failed‮", "severity": "warning", "id": "backup"})
    assert custom.severity is Severity.WARNING and "‮" not in custom.title
    for junk in (None, [], {"alerts": "x"}, {"alerts": [1, "x"]}, {}):
        assert webhooks.parse_alerts(junk) == []


def test_parse_github():
    run = {"action": "completed", "repository": {"full_name": "acme/api", "default_branch": "main"},
           "workflow_run": {"head_branch": "main", "name": "CI", "conclusion": "failure", "html_url": "https://x"}}
    failed = webhooks.parse_github("workflow_run", run)
    assert failed.alert.firing and failed.alert.fingerprint == "gh:wf:acme/api:CI:main"
    run["workflow_run"]["conclusion"] = "success"
    assert not webhooks.parse_github("workflow_run", run).alert.firing
    run["workflow_run"]["head_branch"] = "feature/x"
    assert webhooks.parse_github("workflow_run", run).alert is None  # ветки — не инциденты
    deploy = {"repository": {"full_name": "acme/api"},
              "deployment_status": {"state": "failure", "environment": "prod", "description": "boom"}}
    assert webhooks.parse_github("deployment_status", deploy).alert.severity is Severity.CRITICAL
    deploy["deployment_status"]["state"] = "success"
    ok = webhooks.parse_github("deployment_status", deploy)
    assert not ok.alert.firing and "prod" in ok.notice
    pr = {"action": "closed", "number": 7, "repository": {"full_name": "acme/api"},
          "pull_request": {"title": "Fix", "merged": True, "user": {"login": "ann"}}}
    assert "Влит PR #7" in webhooks.parse_github("pull_request", pr).notice
    assert webhooks.parse_github("push", {}).alert is None and webhooks.parse_github("x", "junk").notice is None


async def team(session):
    now = utcnow()
    for user_id, role in ((1, Role.ADMIN), (2, Role.OPERATOR), (3, Role.OPERATOR), (4, Role.VIEWER)):
        await repo.ensure_member(session, user_id, f"U{user_id}", role, now)
    return {user_id: await repo.get_member(session, user_id) for user_id in (1, 2, 3, 4)}


async def test_ingest_dedup_and_auto_resolve(session):
    event = webhooks.parse_alerts(ALERTMANAGER)[0]
    first = await incidents.ingest(session, event, "alert", utcnow())
    again = await incidents.ingest(session, event, "alert", utcnow())
    assert first.outcome == "created" and again.outcome == "repeated" and again.incident.events == 2
    resolved = await incidents.ingest(session, AlertEvent(event.fingerprint, False, event.title, event.severity, ""),
                                      "alert", utcnow())
    assert resolved.outcome == "resolved" and resolved.incident.status is IncidentStatus.RESOLVED
    assert (await incidents.ingest(session, event, "alert", utcnow())).outcome == "created"  # снова горит — новый
    unknown = AlertEvent("alert:nope", False, "x", Severity.INFO, "")
    assert (await incidents.ingest(session, unknown, "alert", utcnow())).outcome == "ignored"


async def test_roles_and_transitions(session):
    members = await team(session)
    incident = (await incidents.ingest(session, webhooks.parse_alerts(ALERTMANAGER)[0], "alert", utcnow())).incident
    with pytest.raises(OpsError, match="прав"):
        await incidents.act(session, members[4], incident.id, "ack", utcnow())
    with pytest.raises(OpsError, match="не в команде"):
        await incidents.act(session, None, incident.id, "ack", utcnow())
    acked = await incidents.act(session, members[2], incident.id, "ack", utcnow())
    assert acked.status is IncidentStatus.ACKED and acked.acked_by == 2
    with pytest.raises(OpsError, match="уже изменился"):
        await incidents.act(session, members[3], incident.id, "ack", utcnow())
    silenced = await incidents.act(session, members[3], incident.id, "silence", utcnow() - timedelta(hours=2))
    assert silenced.status is IncidentStatus.SILENCED
    assert await repo.silence_expired(session, utcnow()) == [incident.id]  # заглушка кончилась — снова открыт
    done = await incidents.act(session, members[2], incident.id, "resolve", utcnow())
    assert done.status is IncidentStatus.RESOLVED and done.resolved_by == 2
    with pytest.raises(OpsError):
        await incidents.open_manual(session, members[4], "critical", "БД недоступна", utcnow())
    manual = await incidents.open_manual(session, members[2], "warning", "Медленные запросы", utcnow())
    assert manual.severity is Severity.WARNING
    entries = [entry.event for entry in await repo.recent_audit(session, 50)]
    assert {"incident.opened", "incident.ack", "incident.silence", "incident.resolve"} <= set(entries)


async def test_escalation_levels_once(session):
    incident = (await incidents.ingest(session, webhooks.parse_alerts(ALERTMANAGER)[0], "alert", utcnow())).incident
    now = incident.opened_at
    assert await incidents.due_escalations(session, now + timedelta(minutes=5), 10) == []
    [(first, level)] = await incidents.due_escalations(session, now + timedelta(minutes=11), 10)
    assert level == 1 and first.id == incident.id
    assert await incidents.due_escalations(session, now + timedelta(minutes=12), 10) == []
    [(_, level)] = await incidents.due_escalations(session, now + timedelta(minutes=21), 10)
    assert level == 2
    assert await incidents.due_escalations(session, now + timedelta(hours=5), 10) == []


async def test_oncall_rotation_and_override(session):
    members = await team(session)
    with pytest.raises(OpsError):
        await incidents.set_rotation(session, members[2], [2, 3], utcnow())  # не админ
    with pytest.raises(OpsError, match="не оператор"):
        await incidents.set_rotation(session, members[1], [2, 4], utcnow())  # viewer не может дежурить
    await incidents.set_rotation(session, members[1], [2, 3], utcnow())
    now = utcnow()
    first = await incidents.current_oncall(session, now, TZ)
    assert await incidents.current_oncall(session, now + timedelta(days=7), TZ) == ({2, 3} - {first}).pop()
    await incidents.take_shift(session, members[3] if first == 2 else members[2], 4, now)
    assert await incidents.current_oncall(session, now + timedelta(hours=1), TZ) != first
    assert await incidents.current_oncall(session, now + timedelta(hours=5), TZ) == first


async def test_roles_management(session):
    members = await team(session)
    with pytest.raises(OpsError):
        await incidents.set_role(session, members[2], 4, "admin", "x", utcnow())
    with pytest.raises(OpsError, match="самого себя"):
        await incidents.set_role(session, members[1], 1, "viewer", "x", utcnow())
    await incidents.set_role(session, members[1], 50, "operator", "Новый", utcnow())
    assert (await repo.get_member(session, 50)).role is Role.OPERATOR


CATALOG = Catalog.model_validate({"agent_url": "http://agent.internal/run", "actions": [
    {"name": "restart_api", "title": "Перезапуск API", "dangerous": True, "params": {}},
    {"name": "scale", "title": "Масштаб", "dangerous": True, "params": {"count": "^[1-9]$"}},
    {"name": "status", "title": "Статус", "dangerous": False, "params": {}}]})


def test_catalog_validation_and_params():
    assert runbooks.load_catalog().get("restart_api") is not None  # поставляемый actions.json валиден
    for bad in ({"name": "Rm -rf", "title": "xxx"}, {"name": "ok", "title": "xxx", "params": {"a": "("}},
                {"name": "ok", "title": "xxx", "shell": "rm -rf /"}):
        with pytest.raises(ValueError):
            Catalog.model_validate({"agent_url": "http://a/run", "actions": [bad]})
    spec = CATALOG.get("scale")
    assert runbooks.parse_params(spec, ["count=3"]) == {"count": "3"}
    for args in (["count=33"], ["count=3;rm"], ["other=1"], []):
        with pytest.raises(OpsError):
            runbooks.parse_params(spec, args)


async def test_two_person_rule(session):
    members = await team(session)
    quick = await runbooks.request(session, CATALOG, members[2], "status", [], utcnow())
    assert not quick.needs_approval and quick.request.status is ActionStatus.RUNNING
    with pytest.raises(OpsError):
        await runbooks.request(session, CATALOG, members[4], "status", [], utcnow())  # viewer
    with pytest.raises(OpsError, match="Нет такого"):
        await runbooks.request(session, CATALOG, members[2], "drop_database", [], utcnow())

    pending = (await runbooks.request(session, CATALOG, members[2], "restart_api", [], utcnow())).request
    assert pending.status is ActionStatus.PENDING
    with pytest.raises(OpsError, match="другой человек"):
        await runbooks.decide(session, members[2], pending.id, True, utcnow(), 15)
    with pytest.raises(OpsError):
        await runbooks.decide(session, members[4], pending.id, True, utcnow(), 15)
    approved = await runbooks.decide(session, members[3], pending.id, True, utcnow(), 15)
    assert approved.status is ActionStatus.RUNNING and approved.approved_by == 3
    with pytest.raises(OpsError, match="уже решён"):
        await runbooks.decide(session, members[1], pending.id, True, utcnow(), 15)
    finished = await runbooks.finish(session, pending.id, True, "restarted", utcnow())
    assert finished.status is ActionStatus.DONE

    stale = (await runbooks.request(session, CATALOG, members[2], "restart_api", [], utcnow())).request
    with pytest.raises(OpsError, match="истёк"):
        await runbooks.decide(session, members[3], stale.id, True, utcnow() + timedelta(minutes=16), 15)
    assert (await repo.get_request(session, stale.id)).status is ActionStatus.EXPIRED


async def test_agent_signed_request():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True, "output": "done"})

    agent = AgentClient("http://agent.internal/run", "a" * 40, transport=httpx.MockTransport(handler))
    result = await agent.run(7, "scale", {"count": "3"})
    assert result.ok and result.output == "done"
    expected = sign("a" * 40, captured["headers"]["x-timestamp"], captured["body"])
    assert hmac.compare_digest(captured["headers"]["x-signature"], expected)
    assert json.loads(captured["body"]) == {"request_id": 7, "action": "scale", "params": {"count": "3"}}
    await agent.close()

    failing = AgentClient("http://agent.internal/run", "a" * 40,
                          transport=httpx.MockTransport(lambda request: httpx.Response(500, text="oops")))
    result = await failing.run(8, "status", {})
    assert not result.ok and "500" in result.output
    await failing.close()
