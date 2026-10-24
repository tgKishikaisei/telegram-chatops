"""Тесты проверки initData мини-приложения Telegram."""

from __future__ import annotations

import json
import time
from urllib.parse import urlencode

import pytest

from bot.core.telegram_webapp import InitDataError, sign_init_data, validate_init_data

TOKEN = "123456:TEST-TOKEN"


def make_init_data(token: str = TOKEN, auth_date: int | None = None, user_id: int = 42, **extra: str) -> str:
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAH",
        "user": json.dumps({"id": user_id, "first_name": "Ann", "username": "ann"}),
        **extra,
    }
    fields["hash"] = sign_init_data(fields, token)
    return urlencode(fields)


def test_valid_init_data():
    auth = validate_init_data(make_init_data(), TOKEN)
    assert auth.user.id == 42
    assert auth.user.username == "ann"


def test_wrong_token_rejected():
    with pytest.raises(InitDataError):
        validate_init_data(make_init_data(token="999:OTHER"), TOKEN)


def test_tampered_user_rejected():
    data = make_init_data().replace("%22id%22%3A+42", "%22id%22%3A+43")
    with pytest.raises(InitDataError):
        validate_init_data(data, TOKEN)


def test_expired_init_data_rejected():
    with pytest.raises(InitDataError):
        validate_init_data(make_init_data(auth_date=int(time.time()) - 7200), TOKEN, max_age_seconds=3600)


def test_missing_hash_and_duplicates_rejected():
    with pytest.raises(InitDataError):
        validate_init_data("auth_date=1&user=%7B%7D", TOKEN)
    with pytest.raises(InitDataError):
        validate_init_data(make_init_data() + "&auth_date=1", TOKEN)


def test_oversized_rejected():
    with pytest.raises(InitDataError):
        validate_init_data("a" * 5000, TOKEN)
