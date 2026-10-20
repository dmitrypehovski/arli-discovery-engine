"""Объект намерения: типы, происхождение и правила сопоставимости.

До этого шага движок сравнивал признаки, но не спрашивал, о чём вообще намерение.
Здесь появляется то, что делает единицей работы не знакомство двух людей, а
закрытое намерение:

- тип: ``person`` (люди), ``supply`` (есть что предложить), ``demand`` (нужно);
- сопоставимы только встречные типы: спрос с предложением, человек с человеком;
  два предложения или два спроса между собой не совпадение, а конкуренция;
- происхождение: ``human``, ``organisation``, ``synthetic``. Сеть объявляет,
  допускает ли она синтетические намерения, и в человеческой сети синтетическое
  намерение отклоняется с кодом, а не участвует потихоньку.
"""

from __future__ import annotations

from dataclasses import dataclass

INTENT_TYPES = ("person", "supply", "demand")
INTENT_STATUSES = ("active", "paused", "archived")
PROVENANCE = ("human", "organisation", "synthetic")

# Причины отказа намерения. Закрытый перечень: попытка либо проходит, либо
# получает один из этих кодов, и тест проверяет достижимость каждого.
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
    """Намерение не годится к участию. Код из закрытого перечня INTENT_REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in INTENT_REJECTIONS:
            raise ValueError(f"{code!r} is not in INTENT_REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class Intent:
    """Намерение: что человек или организация ищет либо предлагает."""

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
    """Сопоставимы ли типы двух намерений.

    Встречные типы сопоставимы: спрос с предложением. Люди сопоставляются с людьми.
    Два предложения это конкуренция, два спроса это очередь за одним и тем же.
    """
    for value in (first, second):
        if value not in INTENT_TYPES:
            raise IntentRejected("unknown_type", f"type={value!r}")
    if first == second:
        return first == "person"
    return {first, second} == {"supply", "demand"}


def check_admission(intent: Intent, *, admits_synthetic: bool) -> None:
    """Годится ли намерение участвовать в сети. Поднимает отказ с кодом, если нет."""
    intent.validate()
    if intent.provenance == "synthetic" and not admits_synthetic:
        raise IntentRejected(
            "synthetic_not_admitted",
            "this network declares itself human-facing and does not admit synthetic intents",
        )
    if intent.status != "active":
        raise IntentRejected("intent_inactive", f"status={intent.status!r}")
