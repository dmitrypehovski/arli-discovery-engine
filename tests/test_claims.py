"""Trust levels: a claim without a basis is not accepted, and a weak one is not passed off as strong.

The main point here: the engine relays someone else's claim and verifies the issuer's
signature. Who is allowed to issue attestations is a question for a lawyer, and it is not
in the code. So the tests check the mechanics: signature, expiry, state, and the fact that
a level cannot be raised without the issuer's envelope.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from arli_engine.core.claims import (
    CLAIM_LEVELS,
    CLAIM_REJECTIONS,
    ClaimRegistry,
    ClaimRejected,
    FeatureClaim,
    level_strength,
    now_utc,
    strongest,
)
from arli_engine.core.crypto import sign_envelope
from arli_engine.core.signing import KeyPair


def _issued_envelope(issuer_key: KeyPair, *, feature: str = "skill:design", when=None) -> dict:
    """The issuer's own envelope: who, when, what was checked."""
    body = {
        "provider": {"id": "checking-body", "name": "Checking body", "version": "1"},
        "operation": "feature_check",
        "parameters": {"feature": feature, "method": "document review"},
        "normalized_result": {"holds": True},
        "confidence": "observed",
        "authority": "external-service",
        "limitations": ["The check covers the moment of the check only."],
        "captured_at": (when or now_utc()).isoformat(),
    }
    return sign_envelope(body, issuer_key.private_pem, "checking-body-key-1")


def test_levels_have_an_order_and_an_unknown_level_is_refused():
    assert level_strength("declared") < level_strength("third_party_verified")
    assert level_strength("third_party_verified") < level_strength("topic_owner_verified")
    with pytest.raises(ClaimRejected) as exc:
        level_strength("probably-true")
    assert exc.value.code == "unknown_level"
    assert set(CLAIM_LEVELS) == {"declared", "third_party_verified", "topic_owner_verified"}


def test_a_declared_claim_needs_no_issuer_and_verifies():
    registry = ClaimRegistry()
    claim = FeatureClaim(feature="skill:design", checked_at=now_utc())
    ok, reasons = registry.verify_issuer_envelope(claim)
    assert ok is True and reasons == []
    claim.validate()  # a self-asserted level does not require an issuer


def test_a_claim_above_declared_requires_an_issuer_and_its_envelope():
    registry = ClaimRegistry()
    with pytest.raises(ClaimRejected) as exc:
        FeatureClaim(
            feature="skill:design", level="third_party_verified", checked_at=now_utc()
        ).validate()
    assert exc.value.code == "missing_issuer"

    with pytest.raises(ClaimRejected) as exc:
        FeatureClaim(
            feature="skill:design", level="third_party_verified",
            issuer="checking-body", checked_at=now_utc(),
        ).validate()
    assert exc.value.code == "level_upgraded_without_issuer"


def test_an_issuer_envelope_verifies_only_with_the_issuer_key():
    issuer = KeyPair.generate()
    other = KeyPair.generate()
    claim = FeatureClaim(
        feature="skill:design", level="third_party_verified", issuer="checking-body",
        checked_at=now_utc(), issuer_evidence=_issued_envelope(issuer),
    )
    registry = ClaimRegistry()
    registry.add_issuer("checking-body", issuer.public_pem)
    assert registry.verify_issuer_envelope(claim) == (True, [])

    wrong = ClaimRegistry()
    wrong.add_issuer("checking-body", other.public_pem)
    ok, reasons = wrong.verify_issuer_envelope(claim)
    assert ok is False and "issuer_envelope_invalid" in reasons

    unknown = ClaimRegistry()
    ok, reasons = unknown.verify_issuer_envelope(claim)
    assert ok is False and "unknown_issuer" in reasons


def test_a_tampered_issuer_envelope_does_not_hold():
    issuer = KeyPair.generate()
    envelope = _issued_envelope(issuer)
    tampered = dict(envelope)
    tampered["normalized_result"] = {"holds": False}
    claim = FeatureClaim(
        feature="skill:design", level="topic_owner_verified", issuer="checking-body",
        checked_at=now_utc(), issuer_evidence=tampered,
    )
    registry = ClaimRegistry()
    registry.add_issuer("checking-body", issuer.public_pem)
    ok, reasons = registry.verify_issuer_envelope(claim)
    assert ok is False and "issuer_envelope_invalid" in reasons


def test_expiry_and_withdrawal_are_reported_not_silently_dropped():
    issuer = KeyPair.generate()
    registry = ClaimRegistry()
    registry.add_issuer("checking-body", issuer.public_pem)
    moment = now_utc()

    expired = FeatureClaim(
        feature="skill:design", level="third_party_verified", issuer="checking-body",
        checked_at=moment - timedelta(days=400), valid_until=moment - timedelta(days=1),
        issuer_evidence=_issued_envelope(issuer),
    )
    ok, reasons = registry.verify_issuer_envelope(expired)
    assert ok is False and "claim_expired" in reasons

    withdrawn = FeatureClaim(
        feature="skill:design", level="third_party_verified", issuer="checking-body",
        checked_at=moment, issuer_evidence=_issued_envelope(issuer), state="withdrawn",
    )
    ok, reasons = registry.verify_issuer_envelope(withdrawn)
    assert ok is False and "claim_withdrawn" in reasons


def test_a_claim_without_a_moment_is_refused():
    with pytest.raises(ClaimRejected) as exc:
        FeatureClaim(feature="skill:design").validate()
    assert exc.value.code == "missing_checked_at"


def test_the_strongest_claim_is_chosen_and_an_empty_list_is_not_declared():
    weak = FeatureClaim(feature="skill:design", checked_at=now_utc())
    strong = FeatureClaim(
        feature="skill:design", level="topic_owner_verified", issuer="body",
        checked_at=now_utc(), issuer_evidence={"anything": True},
    )
    assert strongest([weak, strong]) is strong
    assert strongest([]) is None


def test_claim_rejections_are_a_closed_set():
    with pytest.raises(ValueError):
        ClaimRejected("made_up")
    for code in ("unknown_level", "level_upgraded_without_issuer", "claim_expired"):
        assert code in CLAIM_REJECTIONS
