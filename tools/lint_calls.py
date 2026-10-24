"""Статическая проверка вызовов репозиториев: первым аргументом всегда передаётся сессия БД.

Ошибка вида `repo.members([1, 2])` вместо `repo.members(session, [1, 2])` не видна юнит-тестам сервисов
(они не заходят в хендлеры и фоновые задачи) и всплывает только в работе. Скрипт находит такие вызовы
по синтаксическому дереву: для каждой функции из bot/repositories/*.py, чей первый параметр `session`,
проверяет все места вызова через импортированный модуль репозитория.

Запуск из корня репозитория: python tools/lint_calls.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def session_functions(bot_dir: Path) -> dict[str, set[str]]:
    """{модуль репозитория: {функции с первым параметром session}}."""
    result: dict[str, set[str]] = {}
    for path in (bot_dir / "bot" / "repositories").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {node.name for node in tree.body
                 if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
                 and node.args.args and node.args.args[0].arg == "session"}
        result[f"bot.repositories.{path.stem}"] = names
    return result


def _aliases(tree: ast.Module, repositories: dict[str, set[str]]) -> dict[str, str]:
    """Имена, под которыми модули репозиториев импортированы в файле: {алиас: модуль}."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.repositories":
            for name in node.names:
                module = f"bot.repositories.{name.name}"
                if module in repositories:
                    aliases[name.asname or name.name] = module
        elif isinstance(node, ast.Import):
            for name in node.names:
                if name.name in repositories and name.asname:
                    aliases[name.asname] = name.name
    return aliases


def _looks_like_session(node: ast.expr) -> bool:
    if isinstance(node, ast.Name):
        return "session" in node.id
    if isinstance(node, ast.Subscript):  # data["session"]
        return isinstance(node.slice, ast.Constant) and node.slice.value == "session"
    if isinstance(node, ast.Attribute):
        return "session" in node.attr
    return False


def check(bot_dir: Path) -> list[str]:
    repositories = session_functions(bot_dir)
    problems = []
    for path in (bot_dir / "bot").rglob("*.py"):
        if "core" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = _aliases(tree, repositories)
        if path.parent.name == "repositories":  # внутри модуля вызовы соседних функций — без алиаса
            aliases_local = {path.stem: f"bot.repositories.{path.stem}"}
        else:
            aliases_local = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            module = None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in aliases:
                module, name = aliases[func.value.id], func.attr
            elif isinstance(func, ast.Name) and aliases_local:
                module, name = next(iter(aliases_local.values())), func.id
            if module is None or name not in repositories.get(module, set()):
                continue
            first = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "session"),
                                                        None)
            if first is None or not _looks_like_session(first):
                problems.append(f"{path.relative_to(ROOT)}:{node.lineno}: {name}() вызван без сессии первым аргументом")
    return problems


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    problems = check(ROOT)
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
