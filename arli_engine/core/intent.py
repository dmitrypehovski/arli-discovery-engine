"""The intent object: types, provenance and comparability rules.

Before this step the engine compared features but never asked what the intent was
even about. This introduces what makes the unit of work a closed intent rather
than an introduction between two people:

- type: ``person`` (people), ``supply`` (something to offer), ``demand`` (a need);
- only opposite types are comparable: demand with supply, a person with a person;
  two offers or two demands facing each other are not a match but competition;
- provenance: ``human``, ``organisation``, ``synthetic``. The network declares
  whether it admits synthetic intents, and in a human network a synthetic intent
  is rejected with a code rather than quietly taking part.
"""

from __future__ import annotations

from dataclasses import dataclass

INTENT_TYPES = ("person", "supply", "demand")
INTENT_STATUSES = ("active", "paused", "archived")
PROVENANCE = ("human", "organisation", "synthetic")

# Reasons an intent is rejected. A closed list: an attempt either passes or gets
# one of these codes, and a test checks that each one is reachable.
INTENT_REJECTIONS = (
    "unknown_type",
    "unknown_status",
    "unknown_provenance",
    "synthetic_not_admitted",
    "intent_inactive",
    "types_not_comparable",
    "network_mismatch",
)


class IntentRejected(RuntimeError):
    """The intent is not fit to take part. The code is from the closed list INTENT_REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in INTENT_REJECTIONS:
            raise ValueError(f"{code!r} is not in INTENT_REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Intent:
    """An intent: what a person or an organisation seeks or offers."""

    intent_id: str
    owner: str
    network: str
    type: str = "person"
    statement: str = ""
    status: str = "active"
    provenance: str = "human"
    jurisdiction: str = ""

    def validate(self) -> None:
        if self.type not in INTENT_TYPES:
            raise IntentRejected("unknown_type", f"type={self.type!r}")
        if self.status not in INTENT_STATUSES:
            raise IntentRejected("unknown_status", f"status={self.status!r}")
        if self.provenance not in PROVENANCE:
            raise IntentRejected("unknown_provenance", f"provenance={self.provenance!r}")


def types_comparable(first: str, second: str) -> bool:
    """Whether the types of two intents are comparable.

    Opposite types are comparable: demand with supply. People match with people.
    Two offers are competition, two demands are a queue for the same thing.
    """
    for value in (first, second):
        if value not in INTENT_TYPES:
            raise IntentRejected("unknown_type", f"type={value!r}")
    if first == second:
        return first == "person"
    return {first, second} == {"supply", "demand"}


def check_admission(intent: Intent, *, admits_synthetic: bool) -> None:
    """Whether the intent is fit to take part in the network. Raises a coded rejection if not."""
    intent.validate()
    if intent.provenance == "synthetic" and not admits_synthetic:
        raise IntentRejected(
            "synthetic_not_admitted",
            "this network declares itself human-facing and does not admit synthetic intents",
        )
    if intent.status != "active":
        raise IntentRejected("intent_inactive", f"status={intent.status!r}")
