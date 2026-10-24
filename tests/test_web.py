"""Приёмник вебхуков через TestClient: токен, подпись GitHub, повтор доставки, outbox."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from bot.config import Settings
from bot.core.database import create_engine, create_session_factory
from bot.repositories import ops as repo
from bot.web.app import create_web_app

ALERT_TOKEN = "t" * 40
GH_SECRET = "g" * 20


@pytest.fixture
def env(tmp_path):
    db_url = f"sqlite+aiosqlite:///{(tmp_path / 'ops.db').as_posix()}"
    settings = Settings(bot_token="123456:TEST", team_chat_id=-100, public_url="https://ops.example",
                        alert_webhook_secret=ALERT_TOKEN, github_webhook_secret=GH_SECRET, agent_secret="a" * 40,
                        database_url=db_url, _env_file=None)
    with TestClient(create_web_app(settings), base_url="https://ops.example") as client:
        yield client, db_url


def query(db_url, action):
    async def runner():
        engine = create_engine(db_url)
        try:
            async with create_session_factory(engine)() as session:
                return await action(session)
        finally:
            await engine.dispose()
    return asyncio.run(runner())


ALERT = {"alerts": [{"status": "firing", "fingerprint": "f1", "labels": {"alertname": "DiskFull",
                                                                        "severity": "critical"}}]}


def test_alert_auth_and_ingest(env):
    client, db_url = env
    assert client.post("/hooks/alerts", json=ALERT).status_code == 401
    assert client.post("/hooks/alerts", json=ALERT, headers={"authorization": "Bearer wrong"}).status_code == 401
    good = {"authorization": f"Bearer {ALERT_TOKEN}"}
    assert client.post("/hooks/alerts", json=ALERT, headers=good).json() == {"ok": True, "processed": 1}
    client.post("/hooks/alerts", json=ALERT, headers=good)
    pending = query(db_url, repo.to_sync)
    assert len(pending) == 1 and pending[0].events == 2 and pending[0].needs_sync  # outbox: ждёт публикации
    assert client.post("/hooks/alerts", content=b"{oops", headers={**good, "content-type": "application/json"}
                       ).status_code == 400


def gh(body: dict, event: str, delivery: str, secret: str = GH_SECRET) -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {"x-hub-signature-256": signature, "x-github-event": event, "x-github-delivery": delivery,
                 "content-type": "application/json"}


def test_github_signature_and_replay(env):
    client, db_url = env
    run = {"action": "completed", "repository": {"full_name": "acme/api", "default_branch": "main"},
           "workflow_run": {"head_branch": "main", "name": "CI", "conclusion": "failure", "html_url": "https://x"}}
    raw, headers = gh(run, "workflow_run", "d-1", secret="w" * 20)
    assert client.post("/hooks/github", content=raw, headers=headers).status_code == 401
    raw, headers = gh(run, "workflow_run", "d-1")
    assert client.post("/hooks/github", content=raw, headers=headers).json() == {"ok": True}
    assert client.post("/hooks/github", content=raw, headers=headers).json()["duplicate"] is True
    assert len(query(db_url, repo.active_incidents)) == 1
    raw, headers = gh({"zen": "hi"}, "ping", "d-2")
    assert client.post("/hooks/github", content=raw, headers=headers).status_code == 200
