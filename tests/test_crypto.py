"""Pair key agreement, commitment binding and envelope signature.

Each check here corresponds to a threat from section 3 of the plan: T2 (a third party),
T12 (replay in another session), T6 (envelope tampering).
"""

from __future__ import annotations

import pytest

from arli_engine.core.crypto import (
    commitment,
    derive_pair_key,
    pair_label,
    sign_envelope,
    verify_envelope,
)
from arli_engine.core.evidence import Evidence
from arli_engine.core.signing import KeyPair


def _envelope(**overrides) -> dict:
    base = dict(
        provider={"id": "test", "name": "test", "version": "1"},
        operation="probe",
        parameters={"side": "agent-a"},
        normalized_result={"matched": 2},
        confidence="derived",
        authority="local-calculation",
        limitations=["test only"],
    )
    base.update(overrides)
    return Evidence(**base).to_json()


def test_pair_key_is_derived_identically_by_both_sides_and_differs_for_a_third():
    a, b, c = KeyPair.generate(), KeyPair.generate(), KeyPair.generate()
    k_ab_a = derive_pair_key(a.private_pem, b.public_pem, session_id="s1",
                             agent_a="agent-a", agent_b="agent-b")
    k_ab_b = derive_pair_key(b.private_pem, a.public_pem, session_id="s1",
                             agent_a="agent-b", agent_b="agent-a")
    k_ac = derive_pair_key(a.private_pem, c.public_pem, session_id="s1",
                           agent_a="agent-a", agent_b="agent-c")
    assert k_ab_a == k_ab_b, "the sides must arrive at one key"
    assert k_ab_a != k_ac, "a third party must get a different key"
    assert len(k_ab_a) == 64


def test_pair_key_changes_with_the_session():
    a, b = KeyPair.generate(), KeyPair.generate()
    first = derive_pair_key(a.private_pem, b.public_pem, session_id="s1",
                            agent_a="agent-a", agent_b="agent-b")
    second = derive_pair_key(a.private_pem, b.public_pem, session_id="s2",
                             agent_a="agent-a", agent_b="agent-b")
    assert first != second, "the key must depend on the session, otherwise a replay is possible"


def test_pair_key_requires_a_session_and_two_agents():
    a, b = KeyPair.generate(), KeyPair.generate()
    with pytest.raises(ValueError):
        derive_pair_key(a.private_pem, b.public_pem, session_id="", agent_a="agent-a", agent_b="agent-b")
    with pytest.raises(ValueError):
        derive_pair_key(a.private_pem, b.public_pem, session_id="s1", agent_a="agent-a", agent_b="agent-a")


def test_pair_label_is_canonical_and_needs_two_agents():
    assert pair_label("agent-a", "agent-b") == pair_label("agent-b", "agent-a")
    with pytest.raises(ValueError):
        pair_label("agent-a", "agent-a")


def test_commitment_is_bound_to_session_and_pair():
    key = "22" * 32
    same = commitment(key, session_id="s1", pair="agent-a|agent-b", feature="skill:design")
    other_session = commitment(key, session_id="s2", pair="agent-a|agent-b", feature="skill:design")
    other_pair = commitment(key, session_id="s1", pair="agent-a|agent-c", feature="skill:design")
    assert same != other_session
    assert same != other_pair
    with pytest.raises(ValueError):
        commitment(key, session_id="", pair="agent-a|agent-b", feature="skill:design")


def test_envelope_signature_verifies_and_tampering_is_caught():
    a = KeyPair.generate()
    signed = sign_envelope(_envelope(), a.private_pem, "agent-a-key-1")
    assert signed["signature"]["alg"] == "ES256"
    ok, errors = verify_envelope(signed, a.public_pem)
    assert ok and errors == []

    tampered = dict(signed)
    tampered["normalized_result"] = {"matched": 99}
    bad, bad_errors = verify_envelope(tampered, a.public_pem)
    assert bad is False
    assert bad_errors

    unsigned = _envelope()
    none_ok, none_errors = verify_envelope(unsigned, a.public_pem)
    assert none_ok is False
    assert none_errors == ["envelope has no signature"]


def test_signature_does_not_change_the_evidence_id():
    """The signature lives next to the body, so the identifier stays reproducible."""
    a = KeyPair.generate()
    unsigned = _envelope()
    signed = sign_envelope(unsigned, a.private_pem, "agent-a-key-1")
    assert signed["evidence_id"] == unsigned["evidence_id"]
    assert Evidence.from_json(signed).verify_id()
    assert Evidence.from_json(signed).signature == signed["signature"]
