"""Настройки ChatOps-бота. Значения — из окружения, /run/secrets и .env (см. .env.example)."""

from __future__ import annotations

from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator

from bot.core.config_types import TimezoneName
from bot.core.settings import BaseBotSettings


class Settings(BaseBotSettings):
    team_chat_id: int                                         # чат команды: инциденты, уведомления, команды
    public_url: str                                           # https-адрес для вебхуков (Caddy)
    alert_webhook_secret: SecretStr = Field(min_length=32)    # Bearer-токен Alertmanager/Grafana
    github_webhook_secret: SecretStr = Field(min_length=16)   # секрет вебхука GitHub (HMAC SHA-256)
    agent_secret: SecretStr = Field(min_length=32)            # подпись запросов к агенту runbook-действий
    timezone: TimezoneName = "Asia/Tashkent"
    escalation_minutes: int = Field(default=10, ge=1, le=240)  # critical без реакции → дежурному в личку
    approval_minutes: int = Field(default=15, ge=1, le=240)    # срок жизни запроса на опасное действие

    @field_validator("public_url")
    @classmethod
    def _https(cls, value: str) -> str:
        value = value.rstrip("/")
        if urlsplit(value).scheme != "https" or not urlsplit(value).hostname:
            raise ValueError("PUBLIC_URL — адрес https://…")
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_url).hostname or ""
