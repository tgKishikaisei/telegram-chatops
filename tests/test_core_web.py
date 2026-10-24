"""Тесты веб-слоя: заголовки безопасности, формат ошибок, лимит тела, Host, авторизация Mini App."""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient
from pydantic import BaseModel

from bot.core.telegram_webapp import WebAppAuth, sign_init_data
from bot.core.web import AppError, TelegramAuth, create_app

TOKEN = "123456:TEST-TOKEN"


class Item(BaseModel):
    name: str


def build_client(production: bool = True) -> TestClient:
    async def ready_db() -> bool:
        return True

    app = create_app(title="t", allowed_hosts=["testserver"], production=production,
                     readiness_checks={"db": ready_db}, max_body_bytes=1024)
    auth = TelegramAuth(TOKEN)

    @app.get("/api/me")
    async def me(user: WebAppAuth = Depends(auth)) -> dict:
        return {"id": user.user.id}

    @app.post("/api/items")
    async def items(item: Item) -> dict:
        return {"name": item.name}

    @app.get("/api/boom")
    async def boom() -> None:
        raise RuntimeError("secret internals")

    @app.get("/api/conflict")
    async def conflict() -> None:
        raise AppError(409, "conflict", "Уже занято")

    return TestClient(app, raise_server_exceptions=False)


def init_data(user_id: int = 7) -> str:
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "A"})}
    fields["hash"] = sign_init_data(fields, TOKEN)
    return urlencode(fields)


@pytest.fixture
def client() -> TestClient:
    return build_client()


def test_health_and_ready(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").json()["checks"] == {"db": True}


def test_security_headers_and_request_id(client):
    response = client.get("/api/conflict")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-request-id"]


def test_app_error_format(client):
    response = client.get("/api/conflict")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conflict"
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


def test_unexpected_error_hides_details(client):
    response = client.get("/api/boom")
    assert response.status_code == 500
    assert "secret" not in response.text
    assert response.json()["error"]["request_id"]


def test_validation_error_does_not_echo_input(client):
    response = client.post("/api/items", json={"name": {"evil": "<script>"}})
    assert response.status_code == 422
    assert "<script>" not in response.text


def test_body_limit(client):
    response = client.post("/api/items", content=b"x" * 2048, headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_untrusted_host_rejected(client):
    assert client.get("/health", headers={"host": "evil.example"}).status_code == 400


def test_auth_required_and_validated(client):
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers={"authorization": "tma bad"}).status_code == 401
    response = client.get("/api/me", headers={"authorization": f"tma {init_data(7)}"})
    assert response.json() == {"id": 7}


def test_docs_disabled_in_production(client):
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert build_client(production=False).get("/openapi.json").status_code == 200
