"""Feature claims: level, issuer and the issuer's own envelope.

The gap this layer closes. An envelope today can say that a party declared these
features about itself. That is true and useless: nobody checks the statement, so
a liar wins matches it did not earn, and a person can be matched with a stand-in
counterparty. Opting out of synthetic participants in human networks is also a
statement, and it too goes unchecked.

What this layer does and does not do. It carries a claim together with its basis
and verifies the basis with the issuer's signature. It does NOT decide who is
allowed to issue attestations: that is a question for a lawyer, and in code it is
left unsolved on purpose. So there is no issuing here, only transfer and
verification of someone else's claim.

Three levels:
- `declared`: the party speaks about itself. No issuer is needed, and there is
  nothing to trust here.
- `third_party_verified`: a third party asserts that the feature held at a point in
  time, and its assertion is itself an envelope.
- `topic_owner_verified`: the owner of the topic, in whose authority this feature falls.

A level is not a property of a person. It is a claim with an issuer. A level does
not prove who stands behind the agent and does not carry over between networks.
Both statements are forbidden in the text and in the checks, because both would be
read as a guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from arli_engine.core.crypto import verify_envelope

# Claim levels. The order matters: it is also the order of strength.
CLAIM_LEVELS = ("declared", "third_party_verified", "topic_owner_verified")

# The state of a claim other than active.
CLAIM_STATES = ("active", "stale", "withdrawn")

CLAIM_REJECTIONS = (
    "unknown_level",
    "unknown_claim_state",
    "missing_issuer",
    "unknown_issuer",
    "issuer_envelope_invalid",
    "claim_expired",
    "claim_withdrawn",
    "level_upgraded_without_issuer",
    "missing_checked_at",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class ClaimRejected(RuntimeError):
    """The claim was rejected. The code is from the closed list CLAIM_REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in CLAIM_REJECTIONS:
            raise ValueError(f"{code!r} is not in CLAIM_REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def level_strength(level: str) -> int:
    """The strength of a level as a number. An unknown level is a rejection, not zero."""
    if level not in CLAIM_LEVELS:
        raise ClaimRejected("unknown_level", f"{level!r} is not a level")
    return CLAIM_LEVELS.index(level)


@dataclass
class FeatureClaim:
    """A claim about a feature together with its basis.

    `issuer_evidence` is the issuer's envelope: who, when, what was checked. For the
    `declared` level it is not needed; for the others the claim is not accepted
    without it.
    """

    feature: str
    level: str = "declared"
    issuer: str = ""
    checked_at: datetime | None = None
    valid_until: datetime | None = None
    issuer_evidence: dict | None = None
    state: str = "active"

    def validate(self) -> None:
        if self.level not in CLAIM_LEVELS:
            raise ClaimRejected("unknown_level", f"{self.level!r} is not a level")
        if self.state not in CLAIM_STATES:
            raise ClaimRejected("unknown_claim_state", f"{self.state!r} is not a state")
        if self.checked_at is None:
            raise ClaimRejected("missing_checked_at", self.feature)
        if self.level == "declared":
            # a self-asserted level cannot require an issuer
            return
        if not self.issuer:
            raise ClaimRejected("missing_issuer", self.feature)
        if self.issuer_evidence is None:
            raise ClaimRejected("level_upgraded_without_issuer", self.feature)

    @property
    def strength(self) -> int:
        return level_strength(self.level)

    def to_json(self) -> dict:
        return {
            "feature": self.feature,
            "level": self.level,
            "issuer": self.issuer,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "valid_until": self.valid_until.isoformat() if self.valid_until else None,
            "state": self.state,
            "has_issuer_evidence": self.issuer_evidence is not None,
        }


@dataclass
class ClaimRegistry:
    """Issuers and their keys. Who exactly is allowed to issue is not decided by this class."""

    issuers: dict[str, bytes] = field(default_factory=dict)

    def add_issuer(self, name: str, public_pem: bytes) -> None:
        if not name:
            raise ClaimRejected("missing_issuer", "issuer name is empty")
        self.issuers[name] = public_pem

    def verify_issuer_envelope(self, claim: FeatureClaim) -> tuple[bool, list[str]]:
        """Verify the basis of the claim. Without a valid issuer signature there is no level."""
        # The common check path: shape first, then expiry, then the signature.
        claim.validate()
        reasons: list[str] = []
        if claim.state == "withdrawn":
            reasons.append("claim_withdrawn")
        if claim.state == "stale":
            reasons.append("claim_expired")
        if claim.valid_until is not None and claim.valid_until <= now_utc():
            reasons.append("claim_expired")
        if claim.level == "declared":
            # there is no level above the declared one, nothing to check
            return (not reasons), reasons
        public_pem = self.issuers.get(claim.issuer)
        if public_pem is None:
            reasons.append("unknown_issuer")
            return False, reasons
        verified, errors = verify_envelope(claim.issuer_evidence or {}, public_pem)
        if not verified:
            reasons.append("issuer_envelope_invalid")
            reasons.extend(errors)
        return (not reasons), reasons

    def to_json(self) -> dict:
        return {"issuers": sorted(self.issuers)}


def strongest(claims: list[FeatureClaim]) -> FeatureClaim | None:
    """The strongest claim in the list. An empty list is None, not "declared"."""
    return max(claims, key=lambda claim: claim.strength) if claims else None
