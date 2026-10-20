"""Честный перечень возможностей агента.

Этот модуль отвечает на дыру, найденную у Index Network: их агенты говорят
что умеют, но не говорят, что при этом произойдёт. Здесь каждая операция
несёт свои эффекты, доступность и ограничения, а отсутствие данных не
трактуется как разрешение.

Форма повторяет ту, что выдаёт `rea capabilities`, которую мы наблюдали
вживую: доступность с кодом причины, набор эффектов и список ограничений.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

# Эффекты операции. Значение None означает "не объявлено" и трактуется
# fail-closed: необъявленный эффект считается возможным.
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
    """Одна операция агента с объявленными последствиями."""

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
        """Эффекты с fail-closed заполнением: необъявленное считается возможным."""
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
    """Перечень как объект для карточки агента и для отдельного запроса."""
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
