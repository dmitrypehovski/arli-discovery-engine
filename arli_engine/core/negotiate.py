"""Bounded agent negotiation and a separate human consent.

The rules are deliberately narrow: the agent negotiates, the human decides. Agent
agreement never opens a connection by itself. Silence, an error and a timeout count
as neither consent nor refusal.

The grammar (as at Index, but with our trail on every transition):
- the initiator makes the first turn: propose or decline;
- accept is possible only immediately after the other side's propose;
- counter is never first and cannot be accepted, it is answered with propose;
- no more than 12 turns; at the limit the negotiation stays undecided, which is not
  a rejection.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from arli_engine.core.evidence import Completeness, Evidence

MAX_TURNS = 12
ACTIONS = ("propose", "counter", "accept", "decline")

# The list of rejection reasons. A closed list: a turn either passes or is rejected
# with one of these codes. A test checks that each code is reachable, otherwise the
# list is just decoration.
REJECTIONS = (
    "not_a_seat",
    "unknown_action",
    "already_settled",
    "turn_limit",
    "not_your_turn",
    "first_turn_is_propose_or_decline",
    "seats_must_alternate",
    "accept_without_offer",
    "propose_over_offer",
    "not_a_witness",
    "witness_without_proof",
    "witness_card_unverified",
    "witness_seat_mismatch",
    "witness_proof_invalid",
    "witness_mismatch",
)

# Reasons a pair does not open at all. Checked before the first turn.
OPENING_REJECTIONS = (
    "intent_inactive",
    "synthetic_not_admitted",
    "same_owner",
    "same_intent",
    "network_mismatch",
    "types_not_comparable",
    "below_required_level",
)

LIMITATIONS = [
    "Turns are free text: the protocol enforces the grammar, not what the agents weigh.",
    "An exhausted negotiation is undecided, not a rejection.",
    "Agent agreement is not principal consent; a separate human act is required.",
]



def witness(
    negotiation: "Negotiation",
    own_name: str,
    seat: str,
    action: str,
    message: str = "",
    proof: dict | None = None,
    card: dict | None = None,
) -> "Turn":
    """Record the counterparty's turn in this journal, on the strength of its own signed card.

    Two machines do not share a journal, so each side has to record what the other side said. The
    card binds a key to a seat and the envelope binds the turn to that key, which is why both are
    required: without them a turn could be written by anyone.
    """
    from arli_engine.core.crypto import verify_envelope
    from arli_engine.core.signing import public_pem_from_card, verify_card_self

    if not isinstance(seat, str) or not seat:
        raise NegotiationRejected("not_a_seat", "a seat is required")
    if seat == own_name:
        raise NegotiationRejected(
            "not_a_witness",
            "an agent takes its own turns with the turn method; witnessing is for the other side",
        )
    if seat not in negotiation.seats:
        raise NegotiationRejected("not_a_seat", f"{seat!r} is not a seat of this negotiation")
    if not isinstance(proof, dict):
        raise NegotiationRejected("witness_without_proof", "the speaker's envelope is required")
    if not isinstance(card, dict):
        raise NegotiationRejected("witness_card_unverified", "the speaker's signed card is required")

    verified, reasons = verify_card_self(card)
    if not verified:
        raise NegotiationRejected(
            "witness_card_unverified", "; ".join(reasons) or "the card does not verify"
        )
    if card.get("name") != seat:
        raise NegotiationRejected(
            "witness_seat_mismatch",
            f"the card names {card.get('name')!r}, the witnessed seat is {seat!r}",
        )
    public_pem = public_pem_from_card(card)
    if not public_pem:
        raise NegotiationRejected("witness_card_unverified", "the card carries no usable public key")
    proved, reasons = verify_envelope(proof, public_pem)
    if not proved:
        raise NegotiationRejected(
            "witness_proof_invalid", "; ".join(reasons) or "the envelope does not verify"
        )
    recorded = proof.get("parameters") or {}
    if recorded.get("seat") != seat or recorded.get("action") != action:
        raise NegotiationRejected(
            "witness_mismatch",
            f"the envelope records seat {recorded.get('seat')!r} and action "
            f"{recorded.get('action')!r}, the request names {seat!r} and {action!r}",
        )
    return negotiation.turn(seat, action, message)

class NegotiationError(RuntimeError):
    """A turn violates the grammar."""


class NegotiationRejected(NegotiationError):
    """A rejection with a code from the closed list REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in REJECTIONS:
            raise ValueError(f"{code!r} is not in REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class OpeningRejected(RuntimeError):
    """The pair is not fit to open. The code is from the closed list OPENING_REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in OPENING_REJECTIONS:
            raise ValueError(f"{code!r} is not in OPENING_REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class IntentStanding:
    """What one needs to know about an intent to decide whether the pair fits.

    An intent fits when it is active, belongs to a participant, is attributed to the
    same network, its provenance is admitted by the network and the two types are
    opposite.
    """

    intent_id: str
    owner: str
    network: str
    active: bool = True
    type: str = "person"
    provenance: str = "human"
    # The claim level about features. Defaults to self-declared: there is nothing to trust here.
    claim_level: str = "declared"


def decide_opening(
    a: IntentStanding,
    b: IntentStanding,
    network: str,
    *,
    admits_synthetic: bool = False,
    required_level: str | None = None,
) -> str | None:
    """Whether the pair is fit to open. None means "fits", otherwise a reason code.

    Checked before the first turn: an inactive intent does not negotiate, a synthetic
    one does not enter a human network, a person is not a pair with itself, the types
    must be opposite (demand with supply, a person with a person) and the network is
    shared.
    """
    from arli_engine.core.claims import level_strength
    from arli_engine.core.intent import types_comparable

    if not a.active or not b.active:
        return "intent_inactive"
    if not admits_synthetic and (a.provenance == "synthetic" or b.provenance == "synthetic"):
        return "synthetic_not_admitted"
    if a.owner == b.owner:
        return "same_owner"
    if a.intent_id == b.intent_id:
        return "same_intent"
    if a.network != network or b.network != network:
        return "network_mismatch"
    if not types_comparable(a.type, b.type):
        return "types_not_comparable"
    if required_level:
        # The network may require a verified feature. Then the declared level is not enough.
        needed = level_strength(required_level)
        if min(level_strength(a.claim_level), level_strength(b.claim_level)) < needed:
            return "below_required_level"
    return None


@dataclass
class Turn:
    seat: str
    action: str
    message: str

    def to_json(self) -> dict:
        return {"seat": self.seat, "action": self.action, "message": self.message}


@dataclass
class Opportunity:
    """A pair of parties and their negotiation, with separate human consents."""

    actors: tuple[str, str]
    negotiation: "Negotiation"
    human_consent: dict[str, bool | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.human_consent:
            self.human_consent = {a: None for a in self.actors}

    @property
    def status(self) -> str:
        outcome = self.negotiation.outcome
        if outcome == "declined":
            return "rejected"
        if outcome != "agreed":
            return "negotiating"
        if all(v is True for v in self.human_consent.values()):
            return "accepted"
        if any(v is False for v in self.human_consent.values()):
            return "rejected"
        return "pending"

    def decide(self, actor: str, approve: bool) -> str:
        """A separate human action. One seat does not decide for the other."""
        if actor not in self.human_consent:
            raise NegotiationError(f"{actor} is not an actor of this opportunity")
        if self.negotiation.outcome != "agreed":
            raise NegotiationError("humans decide only after the agents agreed")
        if self.human_consent[actor] is not None:
            raise NegotiationError(f"{actor} already decided")
        if any(v is False for v in self.human_consent.values()):
            raise NegotiationError("a later yes cannot override an earlier refusal")
        self.human_consent[actor] = approve
        return self.status


class Negotiation:
    """The negotiation log: two seats, one log, no wire between the agents."""

    def __init__(self, seats: tuple[str, str]) -> None:
        if seats[0] == seats[1]:
            raise NegotiationError("two seats must be different")
        self.seats = seats
        self.turns: list[Turn] = []
        self.outcome: str | None = None

    # --- grammar checks ---

    def _validate(self, seat: str, action: str) -> None:
        if seat not in self.seats:
            raise NegotiationRejected("not_a_seat", f"unknown seat: {seat}")
        if action not in ACTIONS:
            raise NegotiationRejected("unknown_action", f"unknown action: {action}")
        if self.outcome is not None:
            raise NegotiationRejected("already_settled", f"negotiation is already {self.outcome}")
        if len(self.turns) >= MAX_TURNS:
            raise NegotiationRejected("turn_limit", f"reached {MAX_TURNS} turns")

        if not self.turns:
            if seat != self.seats[0]:
                raise NegotiationRejected("not_your_turn", "the initiator owes the first turn")
            if action not in ("propose", "decline"):
                raise NegotiationRejected(
                    "first_turn_is_propose_or_decline", f"first turn was {action}"
                )
            return

        last = self.turns[-1]
        if last.seat == seat:
            raise NegotiationRejected("seats_must_alternate", "the other side owes the next turn")
        if action == "accept" and not (last.action == "propose" and last.seat != seat):
            raise NegotiationRejected(
                "accept_without_offer", "accept is legal only directly after the other side's propose"
            )
        if action == "propose" and last.action == "propose" and last.seat != seat:
            raise NegotiationRejected(
                "propose_over_offer", "answer a standing proposal with counter, accept or decline"
            )

    def turn(self, seat: str, action: str, message: str) -> dict:
        """A single turn. Returns the turn trail with an evidence envelope."""
        self._validate(seat, action)
        self.turns.append(Turn(seat=seat, action=action, message=message))
        if action in ("accept", "decline"):
            self.outcome = "agreed" if action == "accept" else "declined"

        completeness = Completeness()
        completeness.dropped_events = {"turns": 0}
        evidence = Evidence(
            provider={"id": "arli-negotiation-grammar", "name": "ARLI bounded negotiation", "version": "1"},
            operation="negotiation_turn",
            parameters={
                "seat": seat,
                "action": action,
                "turn_index": len(self.turns),
                "turn_limit": MAX_TURNS,
            },
            normalized_result={
                "outcome": self.outcome,
                "turn_count": len(self.turns),
                "turns_remaining": MAX_TURNS - len(self.turns),
            },
            confidence="observed",
            authority="external-service",
            limitations=list(LIMITATIONS),
            completeness=completeness,
        )
        return {"turn": self.turns[-1].to_json(), "outcome": self.outcome, "evidence": evidence.to_json()}

    def to_json(self) -> dict:
        return {
            "seats": list(self.seats),
            "outcome": self.outcome,
            "turn_count": len(self.turns),
            "turns": [t.to_json() for t in self.turns],
        }
