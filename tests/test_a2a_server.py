"""Live tests of the HTTP surface: two agents in a thread, checked through httpx."""

from __future__ import annotations

import secrets

import httpx
import pytest

from arli_engine.core.crypto import verify_envelope
from arli_engine.core.psi import MAX_FEATURES_PER_EXCHANGE, feature_hash
from arli_engine.adapters.a2a.cards import build_card
from arli_engine.core.capability import Operation
from arli_engine.core.evidence import Evidence
from arli_engine.core.match import FeatureSet
from arli_engine.adapters.a2a.server import CAPS_PATH, CARD_PATH, RPC_PATH, AgentState, start_agent
from arli_engine.core.signing import KeyPair, verify_card


def _operations() -> list[Operation]:
    return [
        Operation(
            name="discover.match",
            description="match",
            effects={"may_access_network": True, "may_write_filesystem": True},
        ),
        Operation(
            name="directory.impersonate",
            description="act as another principal",
            effects={},
            availability_code="disabled_by_policy",
            availability_reason="Attribution invariant.",
        ),
    ]


@pytest.fixture()
def pair():
    key_a, key_b = KeyPair.generate(), KeyPair.generate()
    neg_store: dict = {}
    opp_store: dict = {}

    def make(name: str, key: KeyPair, port: int, features: list[str]) -> AgentState:
        card = build_card(
            name=name,
            description=f"agent {name}",
            base_url=f"http://127.0.0.1:{port}",
            skills=[{"id": "discover.match", "name": "match", "description": "d",
                     "inputModes": ["application/json"], "outputModes": ["application/json"]}],
            operations=_operations(),
            public_features=features,
            kid=f"{name}-key-1",
            private_pem=key.private_pem,
        )
        return AgentState(name, card, _operations(), FeatureSet(features), "eu-central",
                          negotiations=neg_store, opportunities=opp_store)

    state_a = make("agent-a", key_a, 8911, ["role:founder", "skill:design", "city:sf"])
    state_b = make("agent-b", key_b, 8912, ["skill:design", "city:sf", "role:investor"])
    # The pair key is derived through agreement, so the agents need their own private
    # keys and the other side's public keys.
    state_a.private_pem, state_a.kid = key_a.private_pem, "agent-a-key-1"
    state_a.peer_keys = {"agent-b": key_b.public_pem}
    state_b.private_pem, state_b.kid = key_b.private_pem, "agent-b-key-1"
    state_b.peer_keys = {"agent-a": key_a.public_pem}
    server_a, _ = start_agent(state_a, 8911)
    server_b, _ = start_agent(state_b, 8912)
    yield {"a": (state_a, 8911), "b": (state_b, 8912), "keys": {"agent-a-key-1": key_a, "agent-b-key-1": key_b}}
    server_a.shutdown()
    server_b.shutdown()


def rpc(port: int, method: str, params: dict | None = None) -> dict:
    payload = httpx.post(
        f"http://127.0.0.1:{port}{RPC_PATH}",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        timeout=10,
    ).json()
    return payload


def test_card_is_served_signed_and_verifiable(pair):
    state_b, port_b = pair["b"]
    card = httpx.get(f"http://127.0.0.1:{port_b}{CARD_PATH}", timeout=10).json()
    ok, reasons = verify_card(card, pair["keys"]["agent-b-key-1"].public_pem)
    assert ok and reasons == []
    assert card["interfaces"][0]["protocolBinding"] == "JSONRPC"


def test_capabilities_document_marks_unavailable_operation(pair):
    _, port_b = pair["b"]
    doc = httpx.get(f"http://127.0.0.1:{port_b}{CAPS_PATH}", timeout=10).json()
    unavailable = [o for o in doc["manifest"]["operations"] if not o["available"]]
    assert len(unavailable) == 1
    assert unavailable[0]["availability_reason"]
    assert doc["manifest"]["fail_closed"] is True


def test_matching_over_http_gives_both_sides_their_own_view(pair):
    _, port_a = pair["a"]
    _, port_b = pair["b"]
    session_id = secrets.token_hex(6)

    # The pair key is derived by both sides and never travels over the wire.
    commitments_b = rpc(port_b, "arli/commitments",
                        {"session_id": session_id, "peer_name": "agent-a"})["result"]
    commitments_a = rpc(port_a, "arli/commitments",
                        {"session_id": session_id, "peer_name": "agent-b"})["result"]

    # only commitments go outward, there are no features in them
    assert "skill:design" not in commitments_b["commitments"]

    match_a = rpc(port_a, "arli/match", {
        "session_id": session_id,
        "peer_name": "agent-b",
        "counterpart_commitments": commitments_b["commitments"],
    })["result"]
    match_b = rpc(port_b, "arli/match", {
        "session_id": session_id,
        "peer_name": "agent-a",
        "counterpart_commitments": commitments_a["commitments"],
    })["result"]

    assert match_a["matched_features"] == ["city:sf", "skill:design"]
    assert match_b["matched_features"] == ["city:sf", "skill:design"]
    # no other-side feature that did not match ours is disclosed
    assert "role:investor" not in match_a["matched_features"]
    assert match_a["evidence"]["evidence_id"].startswith("ev_")
    # the envelope is signed with the agent key, and the signature is verified
    ok, reasons = verify_envelope(match_a["evidence"], pair["keys"]["agent-a-key-1"].public_pem)
    assert ok, reasons
    # the count of the other side's features is no longer in the envelope
    assert "counterpart_commitment_count" not in match_a["evidence"]["parameters"]


def test_negotiation_ownership_and_consent_over_http(pair):
    state_a, port_a = pair["a"]
    state_b, port_b = pair["b"]
    neg_id = "neg-test"

    rpc(port_a, "arli/negotiation/open", {"negotiation_id": neg_id, "peers": ["agent-a", "agent-b"]})
    rpc(port_b, "arli/negotiation/open", {"negotiation_id": neg_id, "peers": ["agent-a", "agent-b"]})

    wrong_seat = rpc(port_b, "arli/negotiation/turn", {
        "negotiation_id": neg_id, "seat": "agent-a", "action": "propose", "message": "x",
    })
    # a foreign seat is rejected with a code from the closed list, not free text
    assert "not_a_seat" in wrong_seat["error"]["message"]

    rpc(port_a, "arli/negotiation/turn", {"negotiation_id": neg_id, "seat": "agent-a", "action": "propose", "message": "x"})
    rpc(port_b, "arli/negotiation/turn", {"negotiation_id": neg_id, "seat": "agent-b", "action": "counter", "message": "?"})
    after_counter = rpc(port_a, "arli/negotiation/turn", {
        "negotiation_id": neg_id, "seat": "agent-a", "action": "accept", "message": "",
    })
    assert after_counter["error"]["code"] == -32001  # a grammar refusal
    rpc(port_a, "arli/negotiation/turn", {"negotiation_id": neg_id, "seat": "agent-a", "action": "propose", "message": "y"})
    agreed = rpc(port_b, "arli/negotiation/turn", {"negotiation_id": neg_id, "seat": "agent-b", "action": "accept", "message": "ok"})
    assert agreed["result"]["outcome"] == "agreed"

    pending = rpc(port_a, "arli/opportunity/decide", {"negotiation_id": neg_id, "actor": "agent-a", "approve": True})
    assert pending["result"]["status"] == "pending"
    accepted = rpc(port_b, "arli/opportunity/decide", {"negotiation_id": neg_id, "actor": "agent-b", "approve": True})
    assert accepted["result"]["status"] == "accepted"

    # one negotiation log for both sides
    assert state_a.negotiations[neg_id].to_json() == state_b.negotiations[neg_id].to_json()


def test_unknown_method_and_bad_params(pair):
    _, port_a = pair["a"]
    assert rpc(port_a, "does/not/exist")["error"]["code"] == -32601
    assert rpc(port_a, "arli/match", {"pairing_key": "k"})["error"]["code"] == -32602
    assert rpc(port_a, "arli/negotiation/turn", {"negotiation_id": "nope", "seat": "agent-a", "action": "propose"})["error"]["code"] == -32602


def test_pair_eligibility_is_checked_before_opening(pair):
    """An unfit pair does not open, and the refusal arrives as a code with an envelope.

    At Index such a case is cut silently: the candidate simply disappears. Here a refusal
    is a record with a reason, so the side knows why the pair was not opened.
    """
    port_a = pair["a"][1]
    peers = ["agent-a", "agent-b"]
    # both sides are declared as the same owner: a person is not a pair with itself
    ineligible = [
        {"intent_id": "i1", "owner": "agent-a", "network": "n1"},
        {"intent_id": "i2", "owner": "agent-a", "network": "n1"},
    ]
    refused = rpc(port_a, "arli/negotiation/open", {
        "negotiation_id": "neg-refused", "peers": peers, "network": "n1", "intents": ineligible,
    })["result"]
    assert refused["opened"] is False
    assert refused["rejection"] == "same_owner"
    evidence = refused["evidence"]
    assert Evidence.from_json(evidence).verify_id() is True
    reasons = [e["reason"] for e in evidence["completeness"]["excluded"]]
    assert "refused_before_opening" in reasons

    # an inactive intent does not open either, and the code is different
    inactive = [
        {"intent_id": "i1", "owner": "agent-a", "network": "n1", "active": False},
        {"intent_id": "i2", "owner": "agent-b", "network": "n1", "active": True},
    ]
    refused2 = rpc(port_a, "arli/negotiation/open", {
        "negotiation_id": "neg-inactive", "peers": peers, "network": "n1", "intents": inactive,
    })["result"]
    assert refused2["rejection"] == "intent_inactive"

    # an eligible pair opens, and the opening flag arrives explicitly
    eligible = [
        {"intent_id": "i1", "owner": "agent-a", "network": "n1"},
        {"intent_id": "i2", "owner": "agent-b", "network": "n1"},
    ]
    opened = rpc(port_a, "arli/negotiation/open", {
        "negotiation_id": "neg-ok", "peers": peers, "network": "n1", "intents": eligible,
    })["result"]
    assert opened["opened"] is True
    assert opened["initiator"] == "agent-a"


def test_capability_document_publishes_the_closed_rejection_lists(pair):
    """Another party reads the lists by machine, not from our documentation.

    The published list must match what the code actually throws: otherwise we publish a
    promise we do not keep.
    """
    from arli_engine.core.negotiate import ACTIONS, MAX_TURNS, OPENING_REJECTIONS, REJECTIONS

    port_a = pair["a"][1]
    doc = httpx.get(f"http://127.0.0.1:{port_a}{CAPS_PATH}", timeout=10).json()
    rules = doc["negotiation_rules"]
    assert rules["max_turns"] == MAX_TURNS
    assert rules["actions"] == list(ACTIONS)
    assert rules["turn_rejections"] == list(REJECTIONS)
    assert rules["opening_rejections"] == list(OPENING_REJECTIONS)


def test_closed_intersection_over_http(pair):
    """The whole closed intersection: blinding, signing, unblinding, the holder's record."""
    _, port_a = pair["a"]
    _, port_b = pair["b"]
    session_id = secrets.token_hex(6)

    keys = rpc(port_b, "arli/psi/keys", {"session_id": session_id})["result"]
    assert keys["scheme"] == "closed_blind_rsa"
    commit = rpc(port_a, "arli/psi/commit", {
        "session_id": session_id, "peer_name": "agent-b",
        "n": keys["n"], "e": keys["e"],
    })["result"]
    # only blinded values go outward, there are no features in them
    assert "skill:design" not in str(commit["blinded"])
    signed = rpc(port_b, "arli/psi/sign", {
        "session_id": session_id, "blinded": commit["blinded"],
    })["result"]
    match_a = rpc(port_a, "arli/psi/finish", {
        "session_id": session_id, "n": keys["n"],
        "signed": signed["signed"], "holder_tokens": signed["holder_tokens"],
        "floor_basis_points": 4000,
    })["result"]
    match_b = rpc(port_b, "arli/psi/record", {
        "session_id": session_id, "matched": match_a["matched_features"],
        "floor_basis_points": 4000,
    })["result"]

    assert match_a["matched_features"] == ["city:sf", "skill:design"]
    assert match_b["matched_features"] == ["city:sf", "skill:design"]

    # offline enumeration is closed: the holder's signatures cannot be matched against guesses
    tokens = {int(v, 16) for v in signed["holder_tokens"]}
    n, e = int(keys["n"], 16), int(keys["e"], 16)
    guessed = [g for g in ("skill:design", "city:sf", "role:investor") if pow(feature_hash(g), e, n) in tokens]
    assert guessed == []

    # the applicant's envelope is signed with its key and recomputes
    ok, reasons = verify_envelope(match_a["evidence"], pair["keys"]["agent-a-key-1"].public_pem)
    assert ok, reasons
    assert Evidence.from_json(match_a["evidence"]).verify_id()
    assert match_a["evidence"]["parameters"]["scheme"] == "closed_blind_rsa"
    ok_b, _ = verify_envelope(match_b["evidence"], pair["keys"]["agent-b-key-1"].public_pem)
    assert ok_b


def test_closed_intersection_refuses_over_the_feature_cap(pair):
    """The feature cap: a refusal, not a truncation."""
    _, port_a = pair["a"]
    _, port_b = pair["b"]
    session_id = secrets.token_hex(6)
    keys = rpc(port_b, "arli/psi/keys", {"session_id": session_id})["result"]
    too_many = ["1"] * (MAX_FEATURES_PER_EXCHANGE + 1)
    error = rpc(port_b, "arli/psi/sign", {"session_id": session_id, "blinded": too_many})["error"]
    assert "too_many_features" in error["message"]


def test_match_rejections_from_the_server_side(pair):
    """Three match rejection codes are reachable only at the server layer."""
    _, port_a = pair["a"]
    no_session = rpc(port_a, "arli/psi/commit", {})["error"]["message"]
    assert "missing_pair_key" in no_session
    no_key = rpc(port_a, "arli/psi/commit",
                 {"session_id": "s1", "peer_name": "agent-b"})["error"]["message"]
    assert "missing_peer_key" in no_key
    no_private = rpc(port_a, "arli/psi/sign",
                     {"session_id": "nope", "blinded": ["1"]})["error"]["message"]
    assert "missing_private_key" in no_private
