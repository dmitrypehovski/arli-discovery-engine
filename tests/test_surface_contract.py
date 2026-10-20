"""The declared call contract must match what the handlers actually accept.

Why this test exists. The MCP binding tells a model which fields a tool requires. That list is a
promise, and it was wrong for two methods: it declared `agent` and `act` for a negotiation turn
where the engine wants `seat` and `action`, and `agent` for a decision where the engine wants
`actor` and `approve`. A caller following the declaration got a refusal for a field it had
supplied. The evaluation layer found it; this test keeps it found.
"""

from __future__ import annotations

from arli_engine.adapters.a2a.cards import build_card, capability_document
from arli_engine.adapters.a2a.server import AgentState, start_agent
from arli_engine.client import AgentClient, AgentRefused
from arli_engine.core.capability import EFFECT_KEYS, Operation
from arli_engine.core.match import FeatureSet
from arli_engine.core.signing import KeyPair
from arli_engine.core.surface import CALL_REJECTIONS, METHOD_NAMES, required_fields

# Values for the declared fields, so a call is well formed rather than empty.
VALUES = {
    "negotiation_id": "contract-probe",
    "seat": "arli-contract",
    "action": "propose",
    "actor": "arli-contract",
    "approve": True,
    "kid": "contract-probe",
    "principal_id": "contract",
    "public_pem": "pem",
    "new_kid": "contract-probe-2",
    "new_public_pem": "pem2",
    "reason": "policy",
    "name": "contract-issuer",
    "feature": "skill:contract",
    "level": "declared",
    "evidence_id": "contract-ev",
    "intent_id": "contract-intent",
    "session_id": "contract-session",
    "peer_name": "arli-contract",
    "n": "ff",
    "e": "03",
    "matched": [],
    "features": ["skill:probe"],
    "counterpart_commitments": ["00"],
    "commitment": "00",
    "blinded": ["01"],
    "signed": ["01"],
    "holder_tokens": ["01"],
    "floor_basis_points": 1,
    "peers": ["arli-contract", "other"],
    "intents": [
        {"intent_id": "i1", "owner": "arli-contract", "network": "contract"},
        {"intent_id": "i2", "owner": "other", "network": "contract"},
    ],
    "network": "contract",
    "me": "arli-contract",
}


def _state() -> tuple[AgentState, object]:
    key = KeyPair.generate()
    every = {name: False for name in EFFECT_KEYS}
    operations = [Operation(name="discover.match", description="Match intents", effects=every)]
    card = build_card(
        name="arli-contract",
        description="contract probe",
        base_url="http://127.0.0.1:0",
        skills=[{"id": "discover.match", "name": "match", "description": "d",
                 "inputModes": ["application/json"], "outputModes": ["application/json"]}],
        operations=operations,
        public_features=["skill:design"],
        kid="arli-contract-key",
        private_pem=key.private_pem,
    )
    state = AgentState("arli-contract", card, operations, FeatureSet(["skill:design"]),
                       "eu-central", private_pem=key.private_pem, kid="arli-contract-key")
    server, _thread = start_agent(state, 0)
    return state, server


def test_every_declared_field_has_a_value_to_probe_with():
    """A declared field without a probe value would make this test silent about that method."""
    missing = {
        field for method in METHOD_NAMES for field in required_fields(method) if field not in VALUES
    }
    assert not missing, f"no probe value for {sorted(missing)}"


def test_a_call_with_only_the_declared_fields_never_fails_for_a_missing_field():
    state, server = _state()
    try:
        with AgentClient(f"http://127.0.0.1:{server.server_address[1]}") as client:
            for method in METHOD_NAMES:
                arguments = {field: VALUES[field] for field in required_fields(method)}
                try:
                    client.call(method, arguments)
                except AgentRefused as exc:
                    # A refusal is fine; a refusal about the call contract is not, because the
                    # caller supplied exactly what the surface declared.
                    assert exc.code not in CALL_REJECTIONS, (
                        f"{method} refuses a call built from its own declaration: {exc.message}"
                    )
    finally:
        server.shutdown()
        server.server_close()


def test_the_capability_document_publishes_the_call_refusal_codes():
    every = {name: False for name in EFFECT_KEYS}
    document = capability_document([Operation(name="discover.match", description="d", effects=every)])
    assert document["call_rejections"] == list(CALL_REJECTIONS)
    assert "missing_field" in document["call_rejections"]
