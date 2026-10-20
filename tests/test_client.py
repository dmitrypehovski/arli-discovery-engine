"""Обёртка для внешних клиентов: покрытие методов, разбор ответа, коды отказов.

Обёртка проверяется на живом агенте, а не на заглушке: заглушка показала бы, что клиент
умеет разговаривать с заглушкой. Отдельно проверяется, что причина отказа доходит кодом,
иначе клиентская часть обесценивает работу движка.
"""

from __future__ import annotations

import json

import httpx
import pytest

from arli_engine.adapters.a2a.cards import build_card
from arli_engine.adapters.a2a.server import AgentState, start_agent
from arli_engine.client import AgentClient, AgentClientError, AgentRefused, bound_method_names
from arli_engine.client.cli import main as cli_main
from arli_engine.core.capability import EFFECT_KEYS, Operation
from arli_engine.core.match import FeatureSet
from arli_engine.core.surface import METHOD_NAMES


def _state(name: str) -> AgentState:
    every = {key: False for key in EFFECT_KEYS}
    operations = [
        Operation(name="discover.match", description="Offer and match within a network.", effects=every)
    ]
    card = build_card(
        name=name,
        description="ARLI discovery agent",
        base_url="http://127.0.0.1:0",
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
        name=name, card=card, operations=operations,
        features=FeatureSet(["design:industrial"]), region="eu-central",
    )


@pytest.fixture()
def live_agent():
    state = _state("arli-client-agent")
    server, _thread = start_agent(state, 0)
    port = server.server_address[1]
    yield f"http://127.0.0.1:{port}", state
    server.shutdown()
    server.server_close()


def test_the_client_binds_one_method_per_surface_method():
    assert bound_method_names() == [method.replace("/", "_") for method in METHOD_NAMES]
    for name in bound_method_names():
        assert callable(getattr(AgentClient, name)), name
    assert len(set(bound_method_names())) == len(METHOD_NAMES)


def test_a_call_returns_the_same_result_as_the_engine(live_agent):
    url, _state_value = live_agent
    with AgentClient(url) as client:
        expected = {"kid": "nope", "valid": False, "reasons": ["unknown_key"]}
        assert client.call("arli/registry/check", {"kid": "nope"}) == expected
        # и то же самое через именованный метод, развёрнутый из перечня поверхности
        assert client.arli_registry_check({"kid": "nope"}) == expected


def test_documents_are_reachable_through_the_client(live_agent):
    url, state = live_agent
    with AgentClient(url) as client:
        card = client.get_card()
        capabilities = client.get_capabilities()
    assert card["name"] == "arli-client-agent"
    # признак замкнутого отказа лежит в описании перечня операций, а не на верхнем уровне
    assert capabilities["manifest"]["fail_closed"] is True
    assert capabilities["bindings"]["mcp"]["tools"] == [
        tool.replace("/", "_") for tool in capabilities["bindings"]["a2a"]["methods"]
    ]


def test_a_refusal_keeps_its_code(live_agent):
    url, _state_value = live_agent
    with AgentClient(url) as client:
        client.arli_registry_register(
            {"kid": "k1", "principal_id": "p1", "public_pem": "pem"}
        )
        with pytest.raises(AgentRefused) as exc:
            client.arli_registry_rotate({
                "kid": "k1", "new_kid": "k2", "new_public_pem": "pem2", "reason": "because-i-said-so",
            })
    assert exc.value.code == "unknown_revocation_reason"
    assert "because-i-said-so" in exc.value.message


def test_an_unknown_method_is_refused_locally(live_agent):
    url, _state_value = live_agent
    with AgentClient(url) as client:
        with pytest.raises(ValueError):
            client.call("arli/not_a_method", {})


def test_a_non_json_body_is_reported_not_swallowed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>hello</html>")

    with AgentClient("http://agent.invalid", transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AgentClientError) as exc:
            client.call("arli/registry/check", {"kid": "nope"})
    assert "non-JSON" in str(exc.value)


def test_command_line_prints_the_result_and_returns_zero(live_agent, capsys):
    url, _state_value = live_agent
    assert cli_main([url, "arli/registry/check", json.dumps({"kid": "nope"})]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"kid": "nope", "valid": False, "reasons": ["unknown_key"]}


def test_command_line_separates_a_refusal_from_success(live_agent, capsys):
    url, _state_value = live_agent
    cli_main([url, "arli/registry/register", json.dumps({
        "kid": "k9", "principal_id": "p9", "public_pem": "pem",
    })])
    code = cli_main([url, "arli/registry/revoke", json.dumps({"kid": "k9", "reason": "nonsense"})])
    captured = capsys.readouterr()
    assert code == 3
    assert json.loads(captured.err)["refused"] == "unknown_revocation_reason"


def test_command_line_rejects_bad_usage_without_touching_the_network(capsys):
    assert cli_main([]) == 2
    assert "usage" in capsys.readouterr().err
    assert cli_main(["http://agent.invalid", "arli/registry/check", "{not json"]) == 2
