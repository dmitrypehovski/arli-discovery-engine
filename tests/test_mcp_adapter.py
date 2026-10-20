"""The MCP binding: the same surface, the same refusal code, a different transport.

We check the key property: the binding is not its own surface but a way to reach the
shared one. The tool list matches the method list one to one, a tool result matches the
result of a direct method call, and a refusal reason arrives as the engine's code, not the
word "error".
"""

from __future__ import annotations

import io
import json

import pytest

from arli_engine.adapters.a2a.cards import build_card
from arli_engine.adapters.a2a.server import AgentState
from arli_engine.adapters.mcp.server import (
    PROTOCOL_VERSION,
    McpServer,
    build_tools,
    tool_result_payload,
)
from arli_engine.core.capability import EFFECT_KEYS, Operation
from arli_engine.core.match import FeatureSet
from arli_engine.core.surface import (
    METHOD_NAMES,
    is_mutating,
    method_for_tool,
    required_fields,
    tool_name,
)


def _state(name: str = "arli-mcp-agent") -> AgentState:
    every = {key: False for key in EFFECT_KEYS}
    operations = [
        Operation(name="discover.match", description="Offer and match within a network.", effects=every)
    ]
    card = build_card(
        name=name,
        description="ARLI discovery agent, MCP binding",
        base_url="stdio://mcp",
        skills=[{
            "id": "discover.match",
            "name": "discover.match",
            "description": "Offer and match within a network.",
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        }],
        operations=operations,
        public_features=["design:industrial"],
    )
    return AgentState(
        name=name, card=card, operations=operations, features=FeatureSet(["design:industrial"]), region="eu-central"
    )


def _call(server: McpServer, tool: str, arguments: dict) -> dict:
    response = server.handle({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })
    assert response is not None, "a request must always be answered"
    return response


def test_tool_list_matches_the_surface_one_to_one():
    tools = build_tools()
    assert [tool["name"] for tool in tools] == [tool_name(method) for method in METHOD_NAMES]
    assert len({tool["name"] for tool in tools}) == len(METHOD_NAMES)


def test_each_tool_declares_its_required_fields_and_honest_effects():
    tools = {tool["name"]: tool for tool in build_tools()}
    for method in METHOD_NAMES:
        tool = tools[tool_name(method)]
        assert tool["inputSchema"]["required"] == required_fields(method)
        assert tool["inputSchema"]["additionalProperties"] is True
        note = "This call changes state on the agent."
        assert (note in tool["description"]) is is_mutating(method)


def test_tool_names_translate_back_to_methods_and_unknown_names_are_refused():
    for method in METHOD_NAMES:
        assert method_for_tool(tool_name(method)) == method
    with pytest.raises(ValueError):
        method_for_tool("arli_made_up")


def test_initialize_announces_the_protocol_and_the_server():
    response = McpServer(_state()).handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    result = response["result"]
    assert result["protocolVersion"] == PROTOCOL_VERSION
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"]["name"] == "arli-mcp-agent-mcp"


def test_tool_result_equals_the_direct_method_result():
    server = McpServer(_state())
    direct = server.methods["arli/registry/check"]({"kid": "nope"})
    response = _call(server, "arli_registry_check", {"kid": "nope"})
    assert tool_result_payload(response) == direct == {
        "kid": "nope", "valid": False, "reasons": ["unknown_key"]
    }
    assert response["result"]["isError"] is False


def test_the_capability_document_is_the_same_through_both_bindings():
    server = McpServer(_state())
    direct = server.methods["arli/capabilities"]({})
    via_mcp = tool_result_payload(_call(server, "arli_capabilities", {}))
    assert via_mcp == direct


def test_refusal_keeps_the_engine_code_not_a_generic_error():
    server = McpServer(_state())
    server.methods["arli/registry/register"](
        {"kid": "k1", "principal_id": "p1", "public_pem": "pem"}
    )
    response = _call(server, "arli_registry_rotate", {
        "kid": "k1", "new_kid": "k2", "new_public_pem": "pem2", "reason": "because-i-said-so",
    })
    assert response["result"]["isError"] is True
    assert "unknown_revocation_reason" in response["result"]["content"][0]["text"]


def test_missing_required_field_is_reported_before_the_engine_is_touched():
    server = McpServer(_state())
    response = _call(server, "arli_registry_check", {})
    assert response["result"]["isError"] is True
    assert "missing_field" in response["result"]["content"][0]["text"]
    assert "kid" in response["result"]["content"][0]["text"]


def test_unknown_tool_and_unknown_method_are_protocol_errors():
    server = McpServer(_state())
    unknown_tool = _call(server, "arli_not_a_tool", {})
    assert unknown_tool["error"]["code"] == -32602
    unknown_method = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/whatever"})
    assert unknown_method is not None
    assert unknown_method["error"]["code"] == -32601


def test_stream_transport_answers_requests_and_stays_silent_on_notifications():
    server = McpServer(_state())
    stdin = io.StringIO("\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
        json.dumps({"jsonrpc": "2.0", "id": 3, "method": "ping"}),
        "",
    ]))
    stdout = io.StringIO()
    server.serve_streams(stdin, stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [line["id"] for line in lines] == [1, 2, 3]
    assert len(lines[1]["result"]["tools"]) == len(METHOD_NAMES)
    assert lines[2]["result"] == {}


def test_broken_json_gets_a_parse_error_and_the_stream_continues():
    server = McpServer(_state())
    stdin = io.StringIO("{not json\n" + json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping"}) + "\n")
    stdout = io.StringIO()
    server.serve_streams(stdin, stdout)
    lines = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert lines[0]["error"]["code"] == -32700
    assert lines[1]["id"] == 9
