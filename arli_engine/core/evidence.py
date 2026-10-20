"""Evidence envelope.

Every output of the harness is delivered as an envelope: what exactly was
observed, by what, with what confidence, what was left unverified and what was
dropped by a filter. Absence of data is not treated as good health: sections
closed by policy are listed by name with their reason and count.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from arli_engine.core.canonical import canonical_json

CONFIDENCE_LEVELS = ("observed", "derived", "declared", "unverified")

# How REA treats the owner of a claim: an external service, our own calculation,
# a statement by a party.
AUTHORITIES = ("external-service", "local-calculation", "self-declared")


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class Completeness:
    """Completeness accounting: what is shown, what is dropped and why."""

    status: str = "complete_within_window"
    conditions: list[str] = field(default_factory=list)
    policy_filtered_sections: list[str] = field(default_factory=list)
    truncated_sections: list[str] = field(default_factory=list)
    unavailable_sections: list[str] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    dropped_events: dict[str, int] = field(default_factory=dict)

    def exclude(self, section: str, reason: str, count: int | None = None) -> None:
        if any(e["section"] == section and e["reason"] == reason for e in self.excluded):
            return
        self.excluded.append({"section": section, "reason": reason, "count": count})
        if section not in self.policy_filtered_sections:
            self.policy_filtered_sections.append(section)
        self.status = "policy_filtered"

    def to_json(self) -> dict:
        return {
            "status": self.status,
            "conditions": self.conditions,
            "policy_filtered_sections": sorted(self.policy_filtered_sections),
            "truncated_sections": self.truncated_sections,
            "unavailable_sections": self.unavailable_sections,
            "excluded": self.excluded,
            "dropped_events": self.dropped_events,
        }


@dataclass
class Evidence:
    provider: dict
    operation: str
    parameters: dict
    normalized_result: Any
    confidence: str
    authority: str
    limitations: list[str] = field(default_factory=list)
    completeness: Completeness = field(default_factory=Completeness)
    evidence_id: str = ""
    captured_at: str = ""
    # Signature by the agent key. It is not part of the envelope body, so the
    # identifier stays reproducible while tampering with the body still breaks
    # verification.
    signature: dict | None = None

    def __post_init__(self) -> None:
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(f"unknown confidence: {self.confidence}")
        if self.authority not in AUTHORITIES:
            raise ValueError(f"unknown authority: {self.authority}")
        if not self.captured_at:
            self.captured_at = now_utc()
        if not self.evidence_id:
            self.evidence_id = "ev_" + hashlib.sha256(
                canonical_json(self._body()).encode("utf-8")
            ).hexdigest()

    def _body(self) -> dict:
        return {
            "provider": self.provider,
            "operation": self.operation,
            "parameters": self.parameters,
            "normalized_result": self.normalized_result,
            "confidence": self.confidence,
            "authority": self.authority,
            "limitations": self.limitations,
            "completeness": self.completeness.to_json(),
            "captured_at": self.captured_at,
        }

    def to_json(self) -> dict:
        out = {"evidence_id": self.evidence_id, **self._body()}
        if self.signature is not None:
            out["signature"] = self.signature
        return out

    @classmethod
    def from_json(cls, payload: dict) -> "Evidence":
        """Rebuild an envelope from a record so the id can be rechecked from disk."""
        data = dict(payload)
        evidence_id = data.pop("evidence_id", "")
        completeness = Completeness(**data.pop("completeness", {"excluded": [], "dropped_events": {}}))
        return cls(completeness=completeness, evidence_id=evidence_id, **data)

    def to_json_text(self, indent: int = 1) -> str:
        return json.dumps(self.to_json(), ensure_ascii=False, indent=indent, sort_keys=False)

    def verify_id(self) -> bool:
        """The id must recompute from the envelope body."""
        return self.evidence_id == "ev_" + hashlib.sha256(
            canonical_json(self._body()).encode("utf-8")
        ).hexdigest()
