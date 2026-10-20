"""An honest capability list for an agent.

This module addresses a gap found in Index Network: their agents say what they
can do but not what will happen as a result. Here every operation carries its
effects, availability and limitations, and a missing declaration is not treated
as permission.

The shape follows what `rea capabilities` returns, which we observed live:
availability with a reason code, a set of effects and a list of limitations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# Effects of an operation. A missing value means "not declared" and is treated
# fail-closed: an undeclared effect is assumed possible.
EFFECT_KEYS = (
    "mutates_artifact",
    "launches_process",
    "may_show_ui",
    "may_access_network",
    "may_write_filesystem",
    "changes_permissions",
    "requires_root",
)

AVAILABILITY_CODES = (
    "available",
    "unsupported_host",
    "missing_dependency",
    "not_configured",
    "disabled_by_policy",
)


@dataclass(frozen=True)
class Operation:
    """A single agent operation with its declared consequences."""

    name: str
    description: str
    effects: dict[str, bool]
    limitations: tuple[str, ...] = ()
    availability_code: str = "available"
    availability_reason: str | None = None

    def __post_init__(self) -> None:
        unknown = set(self.effects) - set(EFFECT_KEYS)
        if unknown:
            raise ValueError(f"unknown effect keys: {sorted(unknown)}")
        if self.availability_code not in AVAILABILITY_CODES:
            raise ValueError(f"unknown availability code: {self.availability_code}")
        if self.availability_code != "available" and not self.availability_reason:
            raise ValueError("unavailable operation must carry a reason")

    @property
    def available(self) -> bool:
        return self.availability_code == "available"

    def declared_effects(self) -> dict[str, bool]:
        """Effects with fail-closed filling: an undeclared one is assumed possible."""
        return {k: bool(self.effects.get(k, True)) for k in EFFECT_KEYS}

    def to_json(self) -> dict:
        return {
            "operation": self.name,
            "description": self.description,
            "available": self.available,
            "availability_code": self.availability_code,
            "availability_reason": self.availability_reason,
            "effects": self.declared_effects(),
            "declared_effect_keys": sorted(self.effects),
            "limitations": list(self.limitations),
        }


def capability_manifest(operations: Iterable[Operation]) -> dict:
    """The manifest as an object for the agent card and for a standalone request."""
    ops = list(operations)
    undeclared = [o.name for o in ops if len(o.declared_effects()) != len(o.effects)]
    return {
        "open": any(not o.available for o in ops),
        "operation_count": len(ops),
        "operations": [o.to_json() for o in ops],
        "fail_closed": True,
        "notes": [
            "An effect that is not declared is treated as possible, not as absent.",
            "Unavailable operations stay listed with their reason.",
        ],
        "operations_with_undeclared_effects": undeclared,
    }
