"""Ограниченные переговоры агентов и отдельное согласие человека.

Правила намеренно узкие: агент договаривается, человек решает. Согласие
агента никогда не открывает связь. Молчание, ошибка и тайм-аут не считаются
ни согласием, ни отказом.

Грамматика (как у Index, но с нашим следом на каждом переходе):
- первый ход делает инициатор: propose или decline;
- accept возможен только сразу после propose противной стороны;
- counter никогда не первый и его нельзя принять, на него отвечают propose;
- не больше 12 ходов; на пределе переговоры остаются нерешёнными, это не отказ.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from arli_engine.core.evidence import Completeness, Evidence

MAX_TURNS = 12
ACTIONS = ("propose", "counter", "accept", "decline")

# Перечень причин отказа. Закрытый список: ход либо проходит, либо отклоняется
# с одним из этих кодов. Тест проверяет, что каждый код достижим, иначе список
# превращается в украшение.
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
)

# Причины, по которым пара не открывается вовсе. Проверяются до первого хода.
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


class NegotiationError(RuntimeError):
    """Ход нарушает грамматику."""


class NegotiationRejected(NegotiationError):
    """Отказ с кодом из закрытого перечня REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in REJECTIONS:
            raise ValueError(f"{code!r} is not in REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class OpeningRejected(RuntimeError):
    """Пара не годна к открытию. Код из закрытого перечня OPENING_REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in OPENING_REJECTIONS:
            raise ValueError(f"{code!r} is not in OPENING_REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass(frozen=True)
class IntentStanding:
    """Что нужно знать о намерении, чтобы решить, годится ли пара.

    Намерение годится, когда оно активно, принадлежит участнику, приписано той же
    сети, его происхождение допущено сетью и типы сторон встречные.
    """

    intent_id: str
    owner: str
    network: str
    active: bool = True
    type: str = "person"
    provenance: str = "human"
    # Уровень утверждения о признаках. По умолчанию заявлено самой стороной: верить тут нечему.
    claim_level: str = "declared"


def decide_opening(
    a: IntentStanding,
    b: IntentStanding,
    network: str,
    *,
    admits_synthetic: bool = False,
    required_level: str | None = None,
) -> str | None:
    """Годится ли пара к открытию. None означает «годится», иначе код причины.

    Проверяется до первого хода: неактивное намерение не договаривается, синтетическое
    не входит в человеческую сеть, один человек не пара сам себе, типы должны быть
    встречными (спрос с предложением, человек с человеком), и сеть одна.
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
        # Сеть вправе потребовать подтверждённый признак. Тогда заявленного уровня мало.
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
    """Связка двух сторон и их переговоров, с отдельными согласиями людей."""

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
        """Отдельное действие человека. Одно место не решает за другое."""
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
    """Журнал переговоров: два места, один журнал, никакого провода между агентами."""

    def __init__(self, seats: tuple[str, str]) -> None:
        if seats[0] == seats[1]:
            raise NegotiationError("two seats must be different")
        self.seats = seats
        self.turns: list[Turn] = []
        self.outcome: str | None = None

    # --- проверки грамматики ---

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
        """Один ход. Возвращает след хода с конвертом доказательства."""
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
