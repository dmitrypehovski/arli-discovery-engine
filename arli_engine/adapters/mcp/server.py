"""Привязка MCP: та же поверхность движка в виде инструментов для модели.

Зачем вторая привязка, если уже есть A2A. Мир разделён: агенты разговаривают по A2A,
а среда выполнения модели зовёт инструменты по MCP. Клиент, умеющий только MCP, иначе
не дотянется до движка вовсе, и мы теряем ровно тех, кто мог бы прийти первыми.

Главное свойство, которое здесь защищается: поверхность одна. Привязка не добавляет
и не прячет методов. Список инструментов выводится из общего перечня, и набор проверок
сверяет, что списки обеих привязок совпадают.

Отказы не подменяются: код причины, который движок вернул для метода, попадает в текст
инструмента как есть. Иначе внешний клиент увидит «ошибку» вместо причины и не сможет
отличить «нет совпадений» от «пол не объявлен».
"""

from __future__ import annotations

import json
import sys
from typing import Callable

from arli_engine.adapters.a2a.server import AgentState, _methods
from arli_engine.core.negotiate import NegotiationError
from arli_engine.core.surface import (
    METHOD_NAMES,
    is_mutating,
    method_for_tool,
    required_fields,
    tool_name,
)

PROTOCOL_VERSION = "2024-11-05"

# Методы, которые меняют состояние, помечаются честно: вызывающий должен видеть след.
_MUTATING_NOTE = "This call changes state on the agent."


def determine_description(method: str) -> str:
    parts = [
        f"Calls {method} on the ARLI discovery agent.",
        "Arguments are passed to the engine unchanged.",
        "A refusal keeps the engine's rejection code instead of a generic error.",
    ]
    if is_mutating(method):
        parts.append(_MUTATING_NOTE)
    return " ".join(parts)


def build_tools() -> list[dict]:
    """Список инструментов. Ровно по одному на метод, без добавок и без пропусков."""
    tools = []
    for method in METHOD_NAMES:
        required = required_fields(method)
        tools.append(
            {
                "name": tool_name(method),
                "description": determine_description(method),
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                    "required": required,
                    "additionalProperties": True,
                },
            }
        )
    return tools


def _text(payload: object) -> dict:
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
        "isError": False,
    }


def _error_text(message: str) -> dict:
    return {"content": [{"type": "text", "text": message}], "isError": True}


class McpServer:
    """Сервер MCP поверх общего перечня методов. Транспорт не знает о движке ничего."""

    def __init__(self, state: AgentState, *, name: str | None = None, version: str = "1.0.0") -> None:
        self.state = state
        self.methods: dict[str, Callable[[dict], dict]] = _methods(state)
        self.name = name or f"{state.name}-mcp"
        self.version = version
        # Привязка обязана совпадать с перечнем: расхождение это дефект, а не стиль.
        missing = [m for m in METHOD_NAMES if m not in self.methods]
        extra = [m for m in self.methods if m not in METHOD_NAMES]
        if missing or extra:
            raise RuntimeError(f"surface mismatch: missing={missing} extra={extra}")

    # --- обработка сообщений ---
    def handle(self, message: dict) -> dict | None:
        method = message.get("method")
        req_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": self.name, "version": self.version},
                },
            }
        if method in ("notifications/initialized", "notifications/cancelled"):
            return None
        if method == "ping":
            return {"jsonrpc": "2.0", "id": req_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": req_id, "result": {"tools": build_tools()}}
        if method == "tools/call":
            return self._call(req_id, params)
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"unknown method: {method}"},
        }

    def _call(self, req_id: object, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "tools/call requires a tool name"},
            }
        try:
            method = method_for_tool(name)
        except ValueError:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": f"unknown tool: {name!r}"},
            }
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32602, "message": "arguments must be an object"},
            }
        missing = [field for field in required_fields(method) if field not in arguments]
        if missing:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": _error_text(f"{name}: missing required fields {missing}"),
            }
        try:
            result = self.methods[method](arguments)
        except NegotiationError as exc:
            return {"jsonrpc": "2.0", "id": req_id, "result": _error_text(f"grammar: {exc}")}
        except (ValueError, KeyError) as exc:
            return {"jsonrpc": "2.0", "id": req_id, "result": _error_text(str(exc))}
        except Exception as exc:  # noqa: BLE001
            return {"jsonrpc": "2.0", "id": req_id, "result": _error_text(f"internal: {exc}")}
        return {"jsonrpc": "2.0", "id": req_id, "result": _text(result)}

    # --- транспорт ---
    def serve_streams(self, stdin=None, stdout=None) -> None:
        """Построчный JSON-RPC: так работает штатный потоковый транспорт MCP."""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                stdout.write(json.dumps({
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": f"parse error: {exc}"},
                }) + "\n")
                stdout.flush()
                continue
            response = self.handle(message)
            if response is None:
                continue
            stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
            stdout.flush()


def tool_result_payload(response: dict) -> dict:
    """Развернуть содержимое ответа инструмента обратно в результат движка."""
    return json.loads(response["result"]["content"][0]["text"])


def main(argv: list[str] | None = None) -> int:
    """Запуск привязки как отдельного процесса: python -m arli_engine.adapters.mcp.server."""
    from arli_engine.adapters.a2a.cards import build_card
    from arli_engine.core.capability import EFFECT_KEYS, Operation

    argv = argv if argv is not None else sys.argv[1:]
    name = argv[0] if argv else "arli-agent"
    every = {key: False for key in EFFECT_KEYS}
    operations = [
        Operation(name="discover.match", description="Offer and match within a network.", effects=every)
    ]
    card = build_card(
        name=name,
        description="ARLI discovery agent, MCP binding",
        base_url="stdio://mcp",
        public_features=["design:industrial"],
        skills=[{
            "id": "discover.match",
            "name": "discover.match",
            "description": "Offer and match within a network.",
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        }],
        operations=operations,
    )
    from arli_engine.core.match import FeatureSet

    state = AgentState(
        name=name, card=card, operations=operations, features=FeatureSet(["design:industrial"]), region="eu-central"
    )
    McpServer(state).serve_streams()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
