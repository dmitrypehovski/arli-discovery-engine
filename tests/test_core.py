import pytest

from arli_engine.core.canonical import NotCanonicalizable, canonicalize, canonical_json
from arli_engine.core.capability import EFFECT_KEYS, Operation, capability_manifest
from arli_engine.core.evidence import Completeness, Evidence
from arli_engine.core.match import FeatureSet, local_match
from arli_engine.core.signing import KeyPair, sign_card, verify_card


# --- canonicalization ---

def test_canonical_is_deterministic_and_sorted():
    a = canonical_json({"b": 1, "a": [2, 3], "c": {"z": True, "y": None}})
    b = canonical_json({"c": {"y": None, "z": True}, "a": [2, 3], "b": 1})
    assert a == b
    assert a == '{"a":[2,3],"b":1,"c":{"y":null,"z":true}}'


def test_canonical_rejects_floats():
    with pytest.raises(NotCanonicalizable):
        canonicalize({"overlap": 0.5})


# --- card signature ---

def _card(**overrides):
    card = {"name": "x", "description": "d", "version": "0.1.0", "signatures": []}
    card.update(overrides)
    return card


def test_signature_roundtrip_and_tamper():
    keys = KeyPair.generate()
    signed = sign_card(_card(), keys.private_pem, "k1")
    ok, reasons = verify_card(signed, keys.public_pem)
    assert ok and reasons == []

    tampered = dict(signed)
    tampered["description"] = "other"
    ok, reasons = verify_card(tampered, keys.public_pem)
    assert not ok and reasons


def test_unsinged_card_and_wrong_key_fail():
    keys = KeyPair.generate()
    other = KeyPair.generate()
    assert verify_card(_card(), keys.public_pem)[0] is False
    signed = sign_card(_card(), keys.private_pem, "k1")
    ok, reasons = verify_card(signed, other.public_pem)
    assert not ok and reasons


# --- capabilities ---

def test_effects_fail_closed_for_undeclared_keys():
    op = Operation(name="a", description="d", effects={"may_access_network": False})
    declared = op.declared_effects()
    assert declared["may_access_network"] is False
    assert declared["requires_root"] is True  # an undeclared effect is assumed possible
    assert set(declared) == set(EFFECT_KEYS)
    assert op.to_json()["declared_effect_keys"] == ["may_access_network"]


def test_effects_reject_unknown_key_and_require_reason():
    with pytest.raises(ValueError):
        Operation(name="a", description="d", effects={"teleports": True})
    with pytest.raises(ValueError):
        Operation(name="a", description="d", effects={}, availability_code="disabled_by_policy")
    op = Operation(
        name="a",
        description="d",
        effects={},
        availability_code="disabled_by_policy",
        availability_reason="policy",
    )
    manifest = capability_manifest([op])
    assert manifest["open"] is True and manifest["fail_closed"] is True


# --- evidence ---

def test_evidence_id_recomputes_and_completeness_records_exclusions():
    env = Evidence(
        provider={"id": "p"},
        operation="op",
        parameters={"a": 1},
        normalized_result={"n": 2},
        confidence="observed",
        authority="local-calculation",
    )
    assert env.verify_id()

    completeness = Completeness()
    completeness.exclude("identity", "not_approved", 3)
    assert completeness.status == "policy_filtered"
    assert completeness.to_json()["excluded"] == [
        {"section": "identity", "reason": "not_approved", "count": 3}
    ]


def test_evidence_rejects_unknown_levels():
    with pytest.raises(ValueError):
        Evidence(provider={}, operation="o", parameters={}, normalized_result={},
                 confidence="maybe", authority="local-calculation")
    with pytest.raises(ValueError):
        Evidence(provider={}, operation="o", parameters={}, normalized_result={},
                 confidence="observed", authority="trust-me")


# --- local matching ---

PAIR = "agent-a|agent-b"
PAIR_KEY = "11" * 32  # a deterministic pair key for core tests


def _match(own_features, foreign_features, key=PAIR_KEY, session_id="s1"):
    own = FeatureSet(own_features)
    foreign = FeatureSet(foreign_features)
    outcome = local_match(
        side="agent-a",
        own=own,
        own_commitments=own.commitments(key, session_id=session_id, pair=PAIR),
        counterpart_commitments=sorted(
            foreign.commitments(key, session_id=session_id, pair=PAIR).values()
        ),
        session_id=session_id,
    )
    return own, foreign, outcome


def test_local_match_finds_overlap_and_hides_own_unmatched_features():
    own, foreign, outcome = _match(
        ["role:founder", "skill:design", "city:SF"],
        ["skill:design", " city:sf ", "role:investor"],
    )
    assert outcome.matched_features == ["city:sf", "skill:design"]
    assert outcome.overlap_basis_points == 6667
    assert set(outcome.matched_features).issubset(set(own))
    # there are no other-side features in the result, except those we also hold
    assert not set(outcome.matched_features) - set(own)
    assert outcome.evidence.verify_id()
    assert outcome.evidence.completeness.status == "policy_filtered"


def test_local_match_no_overlap_returns_zero():
    _, _, outcome = _match(["role:founder"], ["role:investor"])
    assert outcome.matched_features == []
    assert outcome.overlap_basis_points == 0


def test_commitments_are_bound_to_the_session_and_the_pair():
    """A change of session splits commitments, while one session draws them together.

    The first closes a replay in another session, the second is mandatory: if the pair
    label or the session do not match between the sides, matching will not work at all.
    """
    own = FeatureSet(["a", "b"])
    other = FeatureSet(["a", "b"])
    other_session = set(own.commitments(PAIR_KEY, session_id="s1", pair=PAIR).values())
    later_session = set(other.commitments(PAIR_KEY, session_id="s2", pair=PAIR).values())
    same_session = set(other.commitments(PAIR_KEY, session_id="s1", pair=PAIR).values())
    assert not other_session & later_session
    assert other_session == same_session
    other_pair = set(other.commitments(PAIR_KEY, session_id="s1", pair="agent-a|agent-c").values())
    assert not other_session & other_pair


# --- negotiation and consent ---

def test_grammar_enforced():
    from arli_engine.core.negotiate import Negotiation, NegotiationError

    neg = Negotiation(seats=("a", "b"))
    with pytest.raises(NegotiationError):
        neg.turn("b", "propose", "")  # the first turn belongs to the initiator
    with pytest.raises(NegotiationError):
        neg.turn("a", "counter", "")  # counter cannot be first

    first = neg.turn("a", "propose", "hi")
    assert first["outcome"] is None
    accepted = neg.turn("b", "accept", "ok")  # accept right after the other side's propose
    assert accepted["outcome"] == "agreed"
    with pytest.raises(NegotiationError):
        neg.turn("a", "decline", "")  # the negotiation is already closed


def test_counter_cannot_be_accepted_and_proposal_blocks_proposal():
    from arli_engine.core.negotiate import Negotiation, NegotiationError

    neg = Negotiation(seats=("a", "b"))
    neg.turn("a", "propose", "one")
    with pytest.raises(NegotiationError):
        neg.turn("b", "propose", "two")  # a proposal over a standing proposal is forbidden
    neg.turn("b", "counter", "why?")
    with pytest.raises(NegotiationError):
        neg.turn("a", "accept", "")  # counter cannot be accepted
    neg.turn("a", "propose", "because")
    assert neg.turn("b", "accept", "ok")["outcome"] == "agreed"


def test_turn_limit_leaves_negotiation_undecided():
    from arli_engine.core.negotiate import MAX_TURNS, Negotiation, NegotiationError

    neg = Negotiation(seats=("a", "b"))
    for i in range(MAX_TURNS):
        seat = "a" if i % 2 == 0 else "b"
        action = "propose" if i % 2 == 0 else "counter"
        neg.turn(seat, action, f"t{i}")
    assert len(neg.turns) == MAX_TURNS
    assert neg.outcome is None  # the limit is not a refusal
    with pytest.raises(NegotiationError):
        neg.turn("a", "propose", "more")


def test_consent_is_separate_from_agreement():
    from arli_engine.core.negotiate import Negotiation, NegotiationError, Opportunity

    neg = Negotiation(seats=("a", "b"))
    opportunity = Opportunity(actors=("a", "b"), negotiation=neg)
    with pytest.raises(NegotiationError):
        opportunity.decide("a", True)  # people decide only after the agents agree

    neg.turn("a", "propose", "x")
    neg.turn("b", "accept", "y")
    assert opportunity.status == "pending"
    assert opportunity.decide("a", True) == "pending"
    with pytest.raises(NegotiationError):
        opportunity.decide("a", True)  # a repeated decision
    assert opportunity.decide("b", True) == "accepted"


def test_later_yes_cannot_override_refusal():
    from arli_engine.core.negotiate import Negotiation, NegotiationError, Opportunity

    neg = Negotiation(seats=("a", "b"))
    neg.turn("a", "propose", "x")
    neg.turn("b", "accept", "y")
    opportunity = Opportunity(actors=("a", "b"), negotiation=neg)
    assert opportunity.decide("a", False) == "rejected"
    with pytest.raises(NegotiationError):
        opportunity.decide("b", True)
