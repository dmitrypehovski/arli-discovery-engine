"""Matching without a third-party holder.

What Index Network did: to tell whether two people's intents match, the network
has to read both intents. Here it is the other way around.

The parties turn their features into commitments and compare them. The pair key
is not transmitted but derived through curve agreement (see core/crypto.py), and
a commitment is bound to the session and to the party. Each side learns only which
of its own features matched.

The honest limitations are recorded in LIMITATIONS and go into the envelope:
- a third party sees no features at all: it does not know the pair key;
- the opposite party does see them, because it must compute its own commitments
  with the same key, and it can enumerate a dictionary of features: this is a
  property of any hash comparison without closed intersection;
- features are few, so an overlap is a signal, not a proof.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from arli_engine.core.crypto import commitment, derive_pair_key, pair_label
from arli_engine.core.evidence import Completeness, Evidence
from arli_engine.core.psi import MAX_FEATURES_PER_EXCHANGE, matched_features

LIMITATIONS = [
    "A counterparty can test guesses about your features; equal cost of both sides is inherent to hash intersection.",
    # The earlier wording was inverted: a coarse feature vocabulary makes the set
    # of guesses small, that is, guessing is easier, not harder. Features are
    # coarse for the sake of match meaning, and that is exactly what lowers the
    # cost of guessing.
    "Feature vocabulary is coarse on purpose, which makes guessing cheaper, not harder; a party that holds the pair key can recover your features from their commitments.",
    "No third party sees both feature sets: the pair key is derived, never transmitted, and matching is computed locally on each side.",
    "The counterparty is not a third party: it derives the same key and can enumerate your features until closed intersection is implemented.",
    "Commitments are bound to the session and to the pair, so they cannot be replayed in another session.",
    "Overlap is a signal, not a proof of fit; the negotiation step exists for that.",
]


def normalize_feature(text: str) -> str:
    """Bring a feature to a comparable form."""
    return " ".join(text.strip().lower().split())


class FeatureSet:
    """The feature set of one party."""

    def __init__(self, features: list[str]) -> None:
        cleaned = {normalize_feature(f) for f in features if normalize_feature(f)}
        if not cleaned:
            raise ValueError("a feature set cannot be empty")
        self._features = sorted(cleaned)

    def __len__(self) -> int:
        return len(self._features)

    def __iter__(self):
        return iter(self._features)

    def commitments(self, pair_key: str, *, session_id: str, pair: str) -> dict[str, str]:
        """Feature commitments under the pair key, bound to the session and to the pair.

        The key arrives ready-made: it is derived through agreement (core/crypto.py)
        and never travels over the wire. The pair label is the same on both sides, so
        the same feature yields the same commitment, while a commitment from another
        session will not match.
        """
        return {
            f: commitment(pair_key, session_id=session_id, pair=pair, feature=f)
            for f in self._features
        }


@dataclass
class MatchOutcome:
    """The result of a local comparison on one side.

    The overlap is expressed as whole basis points: fractional numbers are
    forbidden in the canonical form of evidence, otherwise the envelope
    identifier stops being reproducible.
    """

    matched_features: list[str]
    overlap_basis_points: int
    side: str
    evidence: Evidence

    @property
    def overlap_ratio_text(self) -> str:
        total = self.evidence.parameters["own_feature_count"]
        return f"{len(self.matched_features)}/{total}"

    def to_json(self) -> dict:
        return {
            "side": self.side,
            "matched_features": self.matched_features,
            "overlap_basis_points": self.overlap_basis_points,
            "overlap_ratio": self.overlap_ratio_text,
            "evidence": self.evidence.to_json(),
        }


@dataclass
class PairSession:
    """The pair session: its identifier enters the key derivation and every commitment.

    The key does not travel over the wire: both sides derive it themselves from
    their own signing keys and the shared session identifier.
    """

    session_id: str = field(default_factory=lambda: secrets.token_hex(8))
    disclosed: set[str] = field(default_factory=set)

    def pair_key(
        self,
        own_private_pem: bytes,
        peer_public_pem: bytes,
        *,
        own_name: str,
        peer_name: str,
    ) -> str:
        """Derive the pair key for this session."""
        return derive_pair_key(
            own_private_pem,
            peer_public_pem,
            session_id=self.session_id,
            agent_a=own_name,
            agent_b=peer_name,
        )

    def commitments(
        self,
        features: FeatureSet,
        own_private_pem: bytes,
        peer_public_pem: bytes,
        *,
        own_name: str,
        peer_name: str,
    ) -> dict[str, str]:
        """The commitments of our own features for this session."""
        key = self.pair_key(
            own_private_pem, peer_public_pem, own_name=own_name, peer_name=peer_name
        )
        return features.commitments(
            key, session_id=self.session_id, pair=pair_label(own_name, peer_name)
        )

    def disclose(self, feature: str) -> None:
        self.disclosed.add(normalize_feature(feature))


class MatchRejected(RuntimeError):
    """A match rejection, with a code from the closed list MATCH_REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in MATCH_REJECTIONS:
            raise ValueError(f"{code!r} is not in MATCH_REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# The list of match rejection reasons. A closed list: an attempt either passes or
# gets one of these codes. A test checks that each one is reachable.
MATCH_REJECTIONS = (
    "too_many_features",
    "missing_pair_key",
    "missing_peer_key",
    "missing_private_key",
    "length_mismatch",
    "missing_floor",
    "invalid_floor",
)

# Floor bounds: the overlap is expressed in basis points, 0 means no floor and
# 10000 means full overlap.
FLOOR_MIN = 0
FLOOR_MAX = 10000

# Limitations of closed intersection. What matters here is not to flatter the
# scheme: offline enumeration is closed, but a single guess can still be tested
# online, and that is stated plainly.
CLOSED_LIMITATIONS = [
    "Offline enumeration of the counterparty's features is closed: the learner receives a signature over a feature and cannot match it against guesses without the holder's private key.",
    "Online probing of a single guess remains possible, because any set intersection reveals the result for every element the learner submits; it is bounded by one exchange per session and by the feature cap.",
    "The holder learns how many features were submitted, not their values.",
    "A chosen feature count cap bounds the cost of the exchange; above it matching is refused, not truncated.",
    "Overlap is a signal, not a proof of fit; the negotiation step exists for that.",
]


def closed_match(
    *,
    side: str,
    own: FeatureSet,
    unblinded: list[int],
    holder_tokens: set[int],
    session_id: str,
    floor_basis_points: int,
    policy_filtered: list[tuple[str, str, int | None]] | None = None,
) -> MatchOutcome:
    """Matching by closed intersection (blind RSA signatures).

    A side learns only the matches among its own features. The values obtained from
    the holder are signatures over its features, so another party's set cannot be
    enumerated offline.

    The floor is mandatory and is declared by the caller: without it there is no way
    to tell a good match from a filler. Below the floor the finding is not returned
    at all, rather than being padded up to some number.
    """
    if floor_basis_points is None:
        raise MatchRejected("missing_floor", "floor_basis_points is required")
    if not isinstance(floor_basis_points, int) or not (FLOOR_MIN <= floor_basis_points <= FLOOR_MAX):
        raise MatchRejected(
            "invalid_floor", f"floor must be an integer within {FLOOR_MIN}..{FLOOR_MAX}"
        )
    if len(own) > MAX_FEATURES_PER_EXCHANGE:
        raise MatchRejected("too_many_features", f"{len(own)} features")
    if len(unblinded) != len(own):
        raise MatchRejected(
            "length_mismatch", f"{len(unblinded)} signed values for {len(own)} features"
        )

    features = list(own)
    matched = matched_features(features, unblinded, holder_tokens)
    overlap_basis_points = round(10000 * len(matched) / len(own))
    passed_floor = overlap_basis_points >= floor_basis_points

    completeness = Completeness()
    completeness.exclude("own_unmatched_features", "never_returned_by_design")
    completeness.exclude("counterpart_features", "only_signatures_over_their_features_are_received")
    completeness.exclude(
        "counterpart_feature_count", "not_disclosed_by_us_either: the applicant count is theirs"
    )
    if not passed_floor:
        # A finding below the floor is not returned, and that is recorded, not hidden.
        completeness.exclude("below_floor", "not_returned: overlap is under the declared floor")
    for section, reason, count in policy_filtered or []:
        completeness.exclude(section, reason, count)

    returned = matched if passed_floor else []

    evidence = Evidence(
        provider={
            "id": "arli-closed-intersection",
            "name": "ARLI closed set intersection (blind RSA)",
            "version": "1",
        },
        operation="closed_intersection",
        parameters={
            "session_id": session_id,
            "side": side,
            "own_feature_count": len(own),
            "scheme": "closed_blind_rsa",
            "feature_cap": MAX_FEATURES_PER_EXCHANGE,
            "floor_basis_points": floor_basis_points,
            "passed_floor": passed_floor,
        },
        normalized_result={
            "matched_feature_count": len(returned),
            "overlap_basis_points": overlap_basis_points,
            "matched_features": returned,
        },
        confidence="derived",
        authority="local-calculation",
        limitations=list(CLOSED_LIMITATIONS),
        completeness=completeness,
    )
    return MatchOutcome(
        matched_features=returned,
        overlap_basis_points=overlap_basis_points,
        side=side,
        evidence=evidence,
    )


def closed_match_holder(
    *,
    side: str,
    own: FeatureSet,
    matched: list[str],
    session_id: str,
    floor_basis_points: int,
) -> MatchOutcome:
    """The envelope of the closed-intersection holder.

    The holder computes its own intersection over the names the applicant found and
    records what it learned: the matches, the number of submitted features and the
    floor the parties agreed on.
    """
    names = set(matched)
    found = sorted(f for f in own if f in names)
    overlap_basis_points = round(10000 * len(found) / len(own))

    completeness = Completeness()
    completeness.exclude("applicant_features", "only_blinded_values_were_received")
    completeness.exclude("own_unmatched_features", "never_returned_by_design")

    evidence = Evidence(
        provider={
            "id": "arli-closed-intersection-holder",
            "name": "ARLI closed set intersection, holder side (blind RSA)",
            "version": "1",
        },
        operation="closed_intersection_holder",
        parameters={
            "session_id": session_id,
            "side": side,
            "own_feature_count": len(own),
            "scheme": "closed_blind_rsa",
            "floor_basis_points": floor_basis_points,
        },
        normalized_result={
            "matched_feature_count": len(found),
            "overlap_basis_points": overlap_basis_points,
            "matched_features": found,
        },
        confidence="derived",
        authority="local-calculation",
        limitations=list(CLOSED_LIMITATIONS),
        completeness=completeness,
    )
    return MatchOutcome(
        matched_features=found,
        overlap_basis_points=overlap_basis_points,
        side=side,
        evidence=evidence,
    )


def local_match(
    *,
    side: str,
    own: FeatureSet,
    own_commitments: dict[str, str],
    counterpart_commitments: list[str],
    session_id: str,
    policy_filtered: list[tuple[str, str, int | None]] | None = None,
) -> MatchOutcome:
    """Find matches knowing only our own features and the other side's commitments."""
    foreign = set(counterpart_commitments)
    matched = sorted(f for f, digest in own_commitments.items() if digest in foreign)
    overlap_basis_points = round(10000 * len(matched) / len(own))

    completeness = Completeness()
    # By construction our matcher returns neither our own nor the other side's
    # unmatched features. That is a filter, and it must be visible in the envelope.
    completeness.exclude("own_unmatched_features", "never_returned_by_design")
    completeness.exclude("counterpart_features", "only_bound_commitments_are_received")
    # The count of the other side's features no longer goes into the envelope or
    # the reply: it was a separate leak (T5), and it is closed together with
    # commitment binding.
    completeness.exclude(
        "counterpart_feature_count", "not_disclosed: the count itself leaks the size of their set"
    )
    for section, reason, count in policy_filtered or []:
        completeness.exclude(section, reason, count)

    evidence = Evidence(
        provider={
            "id": "arli-local-matcher",
            "name": "ARLI local commitment intersection",
            "version": "1",
        },
        operation="local_feature_overlap",
        parameters={
            "session_id": session_id,
            "side": side,
            "own_feature_count": len(own),
        },
        normalized_result={
            "matched_feature_count": len(matched),
            "overlap_basis_points": overlap_basis_points,
            "matched_features": matched,
        },
        confidence="derived",
        authority="local-calculation",
        limitations=list(LIMITATIONS),
        completeness=completeness,
    )
    return MatchOutcome(
        matched_features=matched,
        overlap_basis_points=overlap_basis_points,
        side=side,
        evidence=evidence,
    )
