"""Хендлеры ChatOps: команды и кнопки в чате команды (и в личке участников)."""

from aiogram import Router
from aiogram.types import BotCommand

from bot.handlers import ops

COMMANDS = [
    BotCommand(command="status", description="Сводка: инциденты и дежурный"),
    BotCommand(command="incidents", description="Открытые инциденты"),
    BotCommand(command="incident", description="Завести инцидент вручную"),
    BotCommand(command="oncall", description="Дежурства"),
    BotCommand(command="take", description="Взять смену"),
    BotCommand(command="actions", description="Runbook-действия"),
    BotCommand(command="run", description="Запустить действие"),
    BotCommand(command="whoami", description="Мой ID и роль"),
]


def get_routers() -> list[Router]:
    return [ops.router]
