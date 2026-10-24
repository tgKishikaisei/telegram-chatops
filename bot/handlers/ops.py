"""Команды и кнопки ChatOps. Работают только в чате команды и в личке с участниками команды.

Права проверяются в сервисах (incidents.require): viewer — смотреть, operator — действовать,
admin — роли и дежурства. Посторонним бот не отвечает (кроме /whoami — чтобы узнать свой ID).
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import BaseMiddleware, Bot, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import CallbackQuery, Message, TelegramObject
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import Settings
from bot.core.clock import to_local, utcnow
from bot.gateways.agent import AgentClient, AgentError
from bot.keyboards.ops import ActCb, IncCb, approval_keyboard
from bot.models import ActionRequest, Member, Role, Severity
from bot.repositories import ops as repo
from bot.services import incidents, runbooks
from bot.services.incidents import OpsError
from bot.services.runbooks import Catalog

router = Router(name="ops")


class InTeam(BaseFilter):
    """Чат команды или личка с ботом."""

    async def __call__(self, event: Message | CallbackQuery, settings: Settings) -> bool:
        chat = event.chat if isinstance(event, Message) else (event.message.chat if event.message else None)
        return chat is not None and (chat.id == settings.team_chat_id or chat.type == "private")


class MemberMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        user = data.get("event_from_user")
        member = await repo.get_member(data["session"], user.id) if user and "session" in data else None
        if member is not None and user.first_name and member.name != user.first_name[:64]:
            await repo.rename(data["session"], user.id, user.first_name)  # имя — для постов и журнала
            member = await repo.get_member(data["session"], user.id)
        data["member"] = member
        return await handler(event, data)


router.message.filter(InTeam())
router.callback_query.filter(InTeam())


async def _guard(message: Message, member: Member | None) -> bool:
    if member is None:
        return False  # посторонним молчим
    return True


async def _reply_error(message: Message, exc: Exception) -> None:
    await message.reply(f"⛔ {escape(str(exc))}")


@router.message(Command("whoami"))
async def on_whoami(message: Message, member: Member | None) -> None:
    role = member.role.value if member else "не в команде"
    await message.reply(f"Ваш ID: <code>{message.from_user.id}</code>, роль: {role}")


@router.message(Command("start", "help"))
async def on_help(message: Message, member: Member | None) -> None:
    if not await _guard(message, member):
        return
    await message.reply(
        "🛠 <b>ChatOps</b>\n/status — сводка, /incidents — открытые инциденты\n"
        "/incident critical|warning|info текст — завести вручную\n/oncall — кто дежурит, /take [часы] — взять смену\n"
        "/actions — runbook-действия, /run имя [ключ=значение] — запустить (опасные — с подтверждением второго)\n"
        "Админ: /role ID viewer|operator|admin, /oncall_set ID ID…, /audit")


@router.message(Command("status"))
async def on_status(message: Message, session: AsyncSession, member: Member | None, settings: Settings) -> None:
    if not await _guard(message, member):
        return
    active = await repo.active_incidents(session)
    counts = {severity: sum(1 for item in active if item.severity is severity) for severity in Severity}
    oncall = await incidents.current_oncall(session, utcnow(), settings.tz)
    duty = await repo.get_member(session, oncall) if oncall else None
    await message.reply(f"📟 Открыто: 🔴 {counts[Severity.CRITICAL]} · 🟠 {counts[Severity.WARNING]} · "
                        f"🔵 {counts[Severity.INFO]}\nДежурит: {escape(duty.name) if duty else 'не назначен'}")


@router.message(Command("incidents"))
async def on_incidents(message: Message, session: AsyncSession, member: Member | None, settings: Settings) -> None:
    if not await _guard(message, member):
        return
    active = await repo.active_incidents(session)
    if not active:
        await message.reply("🟢 Открытых инцидентов нет.")
        return
    lines = [f"#{item.id} [{item.severity.value}/{item.status.value}] {escape(item.title)} — с "
             f"{to_local(item.opened_at, settings.tz):%d.%m %H:%M}" for item in active]
    await message.reply("\n".join(lines)[:4000])


@router.message(Command("incident"))
async def on_incident(message: Message, command: CommandObject, session: AsyncSession,
                      member: Member | None) -> None:
    if not await _guard(message, member):
        return
    severity, _, title = (command.args or "").partition(" ")
    try:
        incident = await incidents.open_manual(session, member, severity.lower(), title, utcnow())
    except OpsError as exc:
        await _reply_error(message, exc)
        return
    await message.reply(f"Заведён инцидент #{incident.id} — сейчас появится в чате команды.")


@router.callback_query(IncCb.filter())
async def on_incident_action(callback: CallbackQuery, callback_data: IncCb, session: AsyncSession,
                             member: Member | None) -> None:
    try:
        await incidents.act(session, member, callback_data.incident_id, callback_data.action, utcnow())
    except OpsError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer({"ack": "Взято в работу", "resolve": "Решено", "silence": "Заглушено на час"}
                          [callback_data.action])


@router.message(Command("oncall"))
async def on_oncall(message: Message, session: AsyncSession, member: Member | None, settings: Settings) -> None:
    if not await _guard(message, member):
        return
    order = await repo.rotation(session)
    current = await incidents.current_oncall(session, utcnow(), settings.tz)
    names = {item.user_id: item.name for item in await repo.members_with(session, list(Role))}
    if not order:
        await message.reply("Дежурства не настроены. Админ: /oncall_set ID ID …")
        return
    lines = [("👉 " if user_id == current else "• ") + escape(names.get(user_id, str(user_id))) for user_id in order]
    if current not in order and current is not None:
        lines.insert(0, f"👉 {escape(names.get(current, str(current)))} (взял смену)")
    await message.reply("📅 Дежурства (смена — по понедельникам):\n" + "\n".join(lines))


@router.message(Command("oncall_set"))
async def on_oncall_set(message: Message, command: CommandObject, session: AsyncSession,
                        member: Member | None) -> None:
    if not await _guard(message, member):
        return
    parts = (command.args or "").split()
    try:
        if not parts or not all(part.isdigit() for part in parts):
            raise OpsError("Формат: /oncall_set ID ID …")
        await incidents.set_rotation(session, member, [int(part) for part in parts], utcnow())
    except OpsError as exc:
        await _reply_error(message, exc)
        return
    await message.reply("✅ Порядок дежурств сохранён.")


@router.message(Command("take"))
async def on_take(message: Message, command: CommandObject, session: AsyncSession, member: Member | None,
                  settings: Settings) -> None:
    if not await _guard(message, member):
        return
    arg = (command.args or "24").strip()
    try:
        until = await incidents.take_shift(session, member, int(arg) if arg.isdigit() else 0, utcnow())
    except OpsError as exc:
        await _reply_error(message, exc)
        return
    await message.reply(f"✅ {escape(member.name)} дежурит до {to_local(until, settings.tz):%d.%m %H:%M}.")


@router.message(Command("role"))
async def on_role(message: Message, command: CommandObject, session: AsyncSession, member: Member | None) -> None:
    if not await _guard(message, member):
        return
    parts = (command.args or "").split()
    try:
        if len(parts) != 2 or not parts[0].isdigit():
            raise OpsError("Формат: /role ID viewer|operator|admin (ID — из /whoami)")
        name = message.reply_to_message.from_user.first_name if message.reply_to_message else parts[0]
        await incidents.set_role(session, member, int(parts[0]), parts[1].lower(), name, utcnow())
    except OpsError as exc:
        await _reply_error(message, exc)
        return
    await message.reply(f"✅ Роль {escape(parts[1])} для {parts[0]}.")


@router.message(Command("audit"))
async def on_audit(message: Message, session: AsyncSession, member: Member | None, settings: Settings) -> None:
    if not await _guard(message, member):
        return
    try:
        incidents.require(member, Role.ADMIN)
    except OpsError as exc:
        await _reply_error(message, exc)
        return
    entries = await repo.recent_audit(session)
    lines = [f"{to_local(entry.created_at, settings.tz):%d.%m %H:%M} {entry.actor_id or 'система'} "
             f"{escape(entry.event)} {escape(entry.details[:120])}" for entry in entries]
    await message.reply("🧾 <b>Аудит</b>\n" + ("\n".join(lines) or "пусто"))


# ---------- runbook-действия ----------

@router.message(Command("actions"))
async def on_actions(message: Message, member: Member | None, catalog: Catalog) -> None:
    if not await _guard(message, member):
        return
    lines = [f"• <code>{spec.name}</code> — {escape(spec.title)}" + (" ⚠️ нужен второй" if spec.dangerous else "")
             + (f" (параметры: {', '.join(spec.params)})" if spec.params else "") for spec in catalog.actions]
    await message.reply("🧰 <b>Действия</b>\n" + "\n".join(lines) + "\n\nЗапуск: /run имя ключ=значение")


async def execute(bot: Bot, chat_id: int, session: AsyncSession, agent: AgentClient,
                  request: ActionRequest) -> None:
    await session.commit()  # статус running сохранён до сетевого вызова
    try:
        result = await agent.run(request.id, request.action, request.params)
        ok, output = result.ok, result.output
    except AgentError as exc:
        ok, output = False, str(exc)
    done = await runbooks.finish(session, request.id, ok, output, utcnow())
    await session.commit()
    with contextlib.suppress(TelegramAPIError):
        await bot.send_message(chat_id, f"{'✅' if ok else '❌'} Действие #{done.id} <code>{escape(done.action)}</code>"
                                        f":\n<pre>{escape(output[:3000])}</pre>")


@router.message(Command("run"))
async def on_run(message: Message, command: CommandObject, session: AsyncSession, member: Member | None,
                 catalog: Catalog, agent: AgentClient, settings: Settings, bot: Bot) -> None:
    if not await _guard(message, member):
        return
    parts = (command.args or "").split()
    try:
        if not parts:
            raise OpsError("Формат: /run имя [ключ=значение]. Список — /actions")
        requested = await runbooks.request(session, catalog, member, parts[0], parts[1:], utcnow())
    except OpsError as exc:
        await _reply_error(message, exc)
        return
    request = requested.request
    if requested.needs_approval:
        await session.commit()
        await bot.send_message(settings.team_chat_id,
                               f"⚠️ {escape(member.name)} запрашивает <code>{escape(request.action)}</code>"
                               f"{' ' + escape(str(request.params)) if request.params else ''}. "
                               "Нужно подтверждение другого оператора "
                               f"(в течение {settings.approval_minutes} мин).",
                               reply_markup=approval_keyboard(request.id))
        if message.chat.id != settings.team_chat_id:
            await message.reply("Запрос отправлен в чат команды на подтверждение.")
        return
    await message.reply(f"⏳ Выполняю #{request.id}…")
    await execute(bot, message.chat.id, session, agent, request)


@router.callback_query(ActCb.filter())
async def on_decide(callback: CallbackQuery, callback_data: ActCb, session: AsyncSession, member: Member | None,
                    agent: AgentClient, settings: Settings, bot: Bot) -> None:
    try:
        request = await runbooks.decide(session, member, callback_data.request_id, bool(callback_data.approve),
                                        utcnow(), settings.approval_minutes)
    except OpsError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    await callback.answer("Подтверждено" if callback_data.approve else "Отклонено")
    with contextlib.suppress(TelegramAPIError):
        verdict = f"✅ подтвердил {escape(member.name)}" if callback_data.approve else \
            f"✖️ отклонил {escape(member.name)}"
        await callback.message.edit_text(callback.message.html_text + f"\n\n{verdict}", reply_markup=None)
    if callback_data.approve:
        await execute(bot, callback.message.chat.id, session, agent, request)
    else:
        await session.commit()
