"""Хранение и удаление: срок, пометка удаления и физическая чистка.

Что именно хранится у нас: метаданные конвертов доказательства, и ничего больше.
Сырых признаков на стороне сервера нет по построению, поэтому и удалять по требованию
человека надо не признаки, а следы выводов о нём.

Правила:

- у конверта есть срок жизни, по умолчанию 30 дней, и он объявлен;
- архивация намерения помечает связанные конверты удалёнными: они больше не видны
  и не находятся поиском, но запись о том, что конверт был, остаётся до физической чистки;
- физическая чистка убирает записи старше срока целиком.

Пометка и чистка это разные вещи намеренно: пометка мгновенна и обратима только явным
действием, а чистка идёт по сроку.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

RETENTION_DEFAULT_DAYS = 30

RETENTION_REJECTIONS = (
    "unknown_envelope",
    "already_deleted",
    "unknown_intent",
)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


class RetentionRejected(RuntimeError):
    """Операция хранения отклонена. Код из закрытого перечня RETENTION_REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in RETENTION_REJECTIONS:
            raise ValueError(f"{code!r} is not in RETENTION_REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


@dataclass
class EnvelopeRecord:
    evidence_id: str
    intent_id: str
    captured_at: datetime
    deleted_at: datetime | None = None
    deleted_reason: str | None = None

    @property
    def visible(self) -> bool:
        return self.deleted_at is None

    def to_json(self) -> dict:
        return {
            "evidence_id": self.evidence_id,
            "intent_id": self.intent_id,
            "captured_at": self.captured_at.isoformat(),
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
            "deleted_reason": self.deleted_reason,
            "visible": self.visible,
        }


@dataclass
class Retention:
    """Срок жизни конвертов, пометка удаления по намерению и чистка по сроку."""

    days: int = RETENTION_DEFAULT_DAYS
    records: dict[str, EnvelopeRecord] = field(default_factory=dict)

    def store(
        self, evidence_id: str, intent_id: str, *, captured_at: datetime | None = None
    ) -> EnvelopeRecord:
        record = EnvelopeRecord(
            evidence_id=evidence_id, intent_id=intent_id, captured_at=captured_at or now_utc()
        )
        self.records[evidence_id] = record
        return record

    def archive_intent(self, intent_id: str, *, reason: str = "intent_archived") -> int:
        """Пометить конверты намерения удалёнными. Возвращает, сколько помечено."""
        touched = 0
        for record in self.records.values():
            if record.intent_id == intent_id and record.visible:
                record.deleted_at = now_utc()
                record.deleted_reason = reason
                touched += 1
        if touched == 0 and not any(r.intent_id == intent_id for r in self.records.values()):
            raise RetentionRejected("unknown_intent", f"no envelopes for {intent_id!r}")
        return touched

    def delete_envelope(self, evidence_id: str, *, reason: str = "owner_request") -> EnvelopeRecord:
        record = self.records.get(evidence_id)
        if record is None:
            raise RetentionRejected("unknown_envelope", f"{evidence_id!r} is not stored")
        if not record.visible:
            raise RetentionRejected("already_deleted", f"{evidence_id!r} was deleted already")
        record.deleted_at = now_utc()
        record.deleted_reason = reason
        return record

    def visible(self, evidence_id: str) -> bool:
        record = self.records.get(evidence_id)
        return bool(record and record.visible)

    def searchable(self) -> list[str]:
        """Только видимые конверты: поиск не находит удалённые."""
        return sorted(r.evidence_id for r in self.records.values() if r.visible)

    def purge(self, *, at: datetime | None = None) -> int:
        """Физически убрать записи старше срока. Возвращает, сколько убрано."""
        moment = at or now_utc()
        deadline = moment - timedelta(days=self.days)
        stale = [eid for eid, r in self.records.items() if r.captured_at <= deadline]
        for eid in stale:
            del self.records[eid]
        return len(stale)

    def to_json(self) -> dict:
        return {
            "retention_days": self.days,
            "records": [record.to_json() for record in self.records.values()],
            "note": "only envelope metadata is stored; raw features never reach the server",
        }
