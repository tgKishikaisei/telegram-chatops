"""Агент runbook-действий: подписанный HTTP-запрос. Агент (в вашей инфраструктуре) проверяет подпись,
время и одноразовость request_id и выполняет только известные ему действия.

Подпись: X-Signature: sha256=HMAC(secret, "<timestamp>.<тело>"), X-Timestamp — Unix-время.
Агент должен отклонять запросы старше 5 минут и повторы request_id — пример проверки в README.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass

import httpx

MAX_OUTPUT = 3500


class AgentError(Exception):
    pass


@dataclass(frozen=True)
class AgentResult:
    ok: bool
    output: str


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


class AgentClient:
    def __init__(self, url: str, secret: str, *, timeout_seconds: float = 60,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._url = url
        self._secret = secret
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport, follow_redirects=False)

    async def run(self, request_id: int, action: str, params: dict[str, str]) -> AgentResult:
        body = json.dumps({"request_id": request_id, "action": action, "params": params},
                          ensure_ascii=False, sort_keys=True).encode()
        timestamp = str(int(time.time()))
        try:
            response = await self._client.post(self._url, content=body, headers={
                "Content-Type": "application/json", "X-Timestamp": timestamp,
                "X-Signature": sign(self._secret, timestamp, body)})
        except httpx.HTTPError as exc:
            raise AgentError("Агент недоступен") from exc
        if len(response.content) > 64 * 1024:
            raise AgentError("Слишком большой ответ агента")
        try:
            data = response.json()
        except ValueError:
            data = {}
        output = str(data.get("output") or "")[:MAX_OUTPUT] if isinstance(data, dict) else ""
        ok = response.status_code == 200 and isinstance(data, dict) and data.get("ok") is True
        return AgentResult(ok, output or f"HTTP {response.status_code}")

    async def close(self) -> None:
        await self._client.aclose()
