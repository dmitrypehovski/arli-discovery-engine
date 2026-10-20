"""Утверждения о признаках: уровень, выдающий и его собственный конверт.

Дыра, которую закрывает этот слой. Конверт сегодня может сказать: сторона сама заявила эти
признаки о себе. Это правда и это бесполезно: никто не проверяет заявление, поэтому лжец
выигрывает совпадения, которых не заслужил, а человека можно свести с подставным
контрагентом. Отказ от синтетики в человеческих сетях тоже заявление, и он тоже не
проверяется.

Что этот слой делает и чего не делает. Он проводит утверждение вместе с его основанием и
проверяет основание подписью выдающего. Он НЕ решает, кому позволено выдавать
подтверждения: это вопрос к юристу, и в коде он не решён намеренно. Поэтому здесь нет
выдачи, здесь есть перенос и проверка чужого утверждения.

Три уровня:
- `declared`: сторона говорит о себе сама. Выдающий не нужен, и верить тут нечему.
- `third_party_verified`: кто-то третий утверждает, что признак держался на момент времени,
  и его утверждение само является конвертом.
- `topic_owner_verified`: владелец темы, в чьей власти этот признак.

Уровень это не свойство человека. Это утверждение с выдающим. Уровень не доказывает, кто
стоит за агентом, и не переносится между сетями. Обе фразы запрещены и в тексте, и в
проверках, потому что обе были бы прочитаны как гарантия.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from arli_engine.core.crypto import verify_envelope

# Уровни утверждения. Порядок важен: он же порядок силы.
CLAIM_LEVELS = ("declared", "third_party_verified", "topic_owner_verified")

# Состояние утверждения, кроме действующего.
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
    """Утверждение отклонено. Код из закрытого перечня CLAIM_REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in CLAIM_REJECTIONS:
            raise ValueError(f"{code!r} is not in CLAIM_REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def level_strength(level: str) -> int:
    """Сила уровня числом. Неизвестный уровень это отказ, а не ноль."""
    if level not in CLAIM_LEVELS:
        raise ClaimRejected("unknown_level", f"{level!r} is not a level")
    return CLAIM_LEVELS.index(level)


@dataclass
class FeatureClaim:
    """Утверждение о признаке вместе с основанием.

    `issuer_evidence` это конверт выдающего: кто, когда, что проверено. Для уровня `declared`
    он не нужен; для остальных без него утверждение не принимается.
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
            # самозахваченный уровень не может требовать выдающего
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
    """Выдающие и их ключи. Кто именно вправе выдавать, решает не этот класс."""

    issuers: dict[str, bytes] = field(default_factory=dict)

    def add_issuer(self, name: str, public_pem: bytes) -> None:
        if not name:
            raise ClaimRejected("missing_issuer", "issuer name is empty")
        self.issuers[name] = public_pem

    def verify_issuer_envelope(self, claim: FeatureClaim) -> tuple[bool, list[str]]:
        """Проверить основание утверждения. Без верной подписи выдающего уровня нет."""
        # Общий путь проверок: сначала форма, потом срок, потом подпись.
        claim.validate()
        reasons: list[str] = []
        if claim.state == "withdrawn":
            reasons.append("claim_withdrawn")
        if claim.state == "stale":
            reasons.append("claim_expired")
        if claim.valid_until is not None and claim.valid_until <= now_utc():
            reasons.append("claim_expired")
        if claim.level == "declared":
            # уровня выше заявленного нет, проверять нечего
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
    """Самое сильное утверждение из списка. Пустой список это None, а не «заявлено»."""
    return max(claims, key=lambda claim: claim.strength) if claims else None
