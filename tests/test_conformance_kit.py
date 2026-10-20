"""The conformance suite: our own engine passes, the cheats are caught.

The main idea: a suite that passes only on our implementation proves nothing. So the test
requires each cheat to be caught by exactly the check meant for it.
"""

from __future__ import annotations

import pytest

from arli_engine.adapters.a2a.cards import build_card
from arli_engine.adapters.a2a.server import AgentState, start_agent
from arli_engine.conformance import Target, run_conformance
from arli_engine.conformance.fixtures import CheatingAgent, start_cheating_agent
from arli_engine.core.capability import EFFECT_KEYS, Operation
from arli_engine.core.match import FeatureSet
from arli_engine.core.signing import KeyPair


def _operations() -> list[Operation]:
    every = {key: False for key in EFFECT_KEYS}
    every["may_access_network"] = True
    return [
        Operation(name="discover.match", description="Match intents", effects=every),
        Operation(name="act.as.other", description="Act for another principal", effects=every,
                  availability_code="disabled_by_policy",
                  availability_reason="An agent acts only for its own principal."),
    ]


@pytest.fixture()
def engine_pair():
    key_a, key_b = KeyPair.generate(), KeyPair.generate()
    features_a = ["role:founder", "skill:design", "city:sf", "need:infra"]
    features_b = ["skill:design", "city:sf", "offer:infra", "role:investor"]

    def make(name: str, key: KeyPair, port: int, features: list[str]) -> AgentState:
        card = build_card(
            name=name, description=f"agent {name}", base_url=f"http://127.0.0.1:{port}",
            skills=[{"id": "discover.match", "name": "match", "description": "d",
                     "inputModes": ["application/json"], "outputModes": ["application/json"]}],
            operations=_operations(), public_features=features, kid=f"{name}-key-1",
            private_pem=key.private_pem,
        )
        return AgentState(name, card, _operations(), FeatureSet(features), "eu-central",
                          private_pem=key.private_pem, kid=f"{name}-key-1")

    state_a = make("agent-a", key_a, 8961, features_a)
    state_b = make("agent-b", key_b, 8962, features_b)
    state_a.peer_keys = {"agent-b": key_b.public_pem}
    state_b.peer_keys = {"agent-a": key_a.public_pem}
    server_a, _ = start_agent(state_a, 8961)
    server_b, _ = start_agent(state_b, 8962)
    yield 8961, 8962
    server_a.shutdown()
    server_b.shutdown()


def test_our_engine_is_compliant(engine_pair):
    port_a, port_b = engine_pair
    target = Target(base_url=f"http://127.0.0.1:{port_a}", name="agent-a")
    holder = Target(base_url=f"http://127.0.0.1:{port_b}", name="agent-b")
    try:
        report = run_conformance(target, holder)
    finally:
        target.close()
        holder.close()
    assert report.failures == [], report.failures
    assert report.compliant
    # the suite is not empty: there must be many checks, otherwise "conformance" means nothing
    assert len(report.checks) >= 14


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("leaky", {"card.self_verified", "caps.intersection_section", "match.no_counterparty_count"}),
        ("unsigned", {"evidence.signed"}),
        ("padding", {"match.symmetric"}),
    ],
)
def test_cheating_targets_are_caught(kind, expected):
    applicant = CheatingAgent(kind, "cheat-a", 8971, ["skill:design", "city:sf"])
    holder = CheatingAgent(kind, "cheat-b", 8972, ["skill:design", "city:sf", "role:investor"])
    servers = [start_cheating_agent(applicant), start_cheating_agent(holder)]
    target = Target(base_url="http://127.0.0.1:8971", name="cheat-a")
    other = Target(base_url="http://127.0.0.1:8972", name="cheat-b")
    try:
        report = run_conformance(target, other)
        codes = {c["code"] for c in report.failures}
        assert not report.compliant, f"cheat {kind} passed the suite"
        assert expected <= codes, f"expected {expected}, caught {codes}"
    finally:
        for server in servers:
            server.shutdown()
        target.close()
        other.close()
