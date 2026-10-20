"""Адаптер плагина: опись инструментов для установки в среду пользователя.

Плагин не заводит собственный код движка и не ходит в сеть напрямую. Он запускает ту же
привязку MCP отдельным процессом и объявляет её инструменты. Поэтому кода здесь почти нет,
а есть опись: имя, версия, команда запуска, перечень инструментов и честные ограничения.

Опись не пишется руками. Рукописная опись расходится с движком на первой же правке, и
расходится молча: установленный плагин начнёт звать инструмент, которого больше нет.
Поэтому опись собирается из общего перечня поверхности, а набор проверок сверяет, что
записанный файл совпадает с собранным.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from arli_engine.adapters.mcp.server import build_tools
from arli_engine.core.surface import METHOD_NAMES, tool_name

PLUGIN_NAME = "arli-discovery"
PLUGIN_VERSION = "0.1.0"
MANIFEST_FILENAME = "arli-discovery.json"

# Команда запуска: тот же пакет, что и привязка MCP. Плагин не дублирует движок.
SERVER_COMMAND = (sys.executable, "-m", "arli_engine.adapters.mcp.server")

LIMITATIONS = [
    "The plugin runs the MCP binding as a local process; it holds no engine logic of its own.",
    "Features never leave the agent: the plugin cannot read raw feature values.",
    "Matching is not a search: a smaller intersection is a smaller answer, not a worse one.",
]


def build_manifest(*, command: tuple[str, ...] = SERVER_COMMAND) -> dict:
    """Собрать опись из поверхности. Один источник, никаких рукописных списков."""
    tools = build_tools()
    return {
        "name": PLUGIN_NAME,
        "version": PLUGIN_VERSION,
        "description": (
            "ARLI discovery: an agent that matches on declared intent without an index holder. "
            "Ships the A2A surface to a model runtime as MCP tools."
        ),
        "transport": {"type": "stdio", "jsonrpc": "2.0", "framing": "newline-delimited"},
        "server": {"command": list(command), "env": {}},
        "tools": tools,
        "surface": {
            "methods": list(METHOD_NAMES),
            "tool_names": [tool_name(method) for method in METHOD_NAMES],
            "rule": "generated from the engine surface, never hand-written",
        },
        "limitations": list(LIMITATIONS),
    }


def manifest_json(*, command: tuple[str, ...] = SERVER_COMMAND) -> str:
    return json.dumps(build_manifest(command=command), ensure_ascii=False, indent=2) + "\n"


def manifest_path() -> Path:
    return Path(__file__).with_name(MANIFEST_FILENAME)


def write_manifest(path: Path | None = None) -> Path:
    target = path or manifest_path()
    target.write_text(manifest_json(), encoding="utf-8")
    return target
