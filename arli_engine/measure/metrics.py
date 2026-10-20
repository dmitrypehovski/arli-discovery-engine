"""Измеритель: числа из конвертов, а не из счётчика, который можно поправить руками.

Зачем это отдельным слоем. Обещание «у каждого вывода есть проверяемое доказательство» можно
подтвердить только по конвертам. Счётчик, который растёт при каждом выводе, ничего не
подтверждает: он подтверждает, что кто-то вызвал метод.

Два правила, из которых здесь всё вытекает.

Первое: конверт без верной подписи доказательством не считается. Иначе покрытие
доказательствами измеряло бы наличие поля, а не проверяемость.

Второе: неизмеренная величина сообщается как неизмеренная, с причиной. Ноль вместо
«не знаем» это худший вид вранья, потому что выглядит как результат. Поэтому у каждой
величины есть состояние: измерено или нет, и во втором случае причина.

Третье, отдельно: числа пилота берутся только с согласившихся людей. Этот слой считает числа
из того, что ему дали. Он не заменяет пилот, он его инструмент.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable

from arli_engine.core.crypto import verify_envelope
from arli_engine.core.evidence import Evidence
from arli_engine.core.intent import INTENT_REJECTIONS
from arli_engine.core.match import MATCH_REJECTIONS
from arli_engine.core.negotiate import OPENING_REJECTIONS, REJECTIONS
from arli_engine.core.registry import REGISTRY_REJECTIONS
from arli_engine.core.retention import RETENTION_REJECTIONS

MEASURED = "measured"
NOT_MEASURED = "not_measured"

MEASURE_REJECTIONS = (
    "unknown_metric",
    "invalid_value",
    "unknown_refusal_code",
)


class MeasureRejected(RuntimeError):
    """Величину измерить нельзя. Код из закрытого перечня MEASURE_REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in MEASURE_REJECTIONS:
            raise ValueError(f"{code!r} is not in MEASURE_REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# Все коды отказов движка: по ним считается полнота причин.
KNOWN_REFUSAL_CODES: tuple[str, ...] = (
    tuple(REJECTIONS)
    + tuple(OPENING_REJECTIONS)
    + tuple(MATCH_REJECTIONS)
    + tuple(RETENTION_REJECTIONS)
    + tuple(REGISTRY_REJECTIONS)
    + tuple(INTENT_REJECTIONS)
)


@dataclass
class Metric:
    name: str
    state: str
    value: float | None = None
    unit: str = ""
    basis: str = ""
    reason: str = ""

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "value": self.value,
            "unit": self.unit,
            "basis": self.basis,
            "reason": self.reason,
        }


def measured(name: str, value: float, *, unit: str, basis: str) -> Metric:
    return Metric(name=name, state=MEASURED, value=value, unit=unit, basis=basis)


def not_measured(name: str, *, basis: str, reason: str) -> Metric:
    return Metric(name=name, state=NOT_MEASURED, value=None, basis=basis, reason=reason)


def envelope_is_evidence(envelope: dict, public_pem: bytes | None) -> bool:
    """Доказательство это конверт с пересчитываемым идентификатором и верной подписью.

    Подпись проверяется открытым ключом того, кто конверт выдал. Если ключа нет, конверт
    доказательством не считается: подпись без ключа это украшение, а не проверка.
    """
    if not isinstance(envelope, dict) or public_pem is None:
        return False
    if envelope.get("signature") is None:
        return False
    try:
        parsed = Evidence.from_json(envelope)
    except Exception:  # noqa: BLE001 — чужой конверт может быть любой формы
        return False
    if not parsed.verify_id():
        return False
    verified, _errors = verify_envelope(envelope, public_pem)
    return bool(verified)


# --- величины ---

def evidence_coverage(
    envelopes: Iterable[dict], public_keys: dict[str, bytes] | None = None
) -> Metric:
    """Доля выводов и отказов, у которых есть проверяемый конверт.

    Ключи передаются словарём: имя выдающего к публичному ключу. Конверт от выдающего без
    ключа считается непроверенным, и величина об этом честно сообщает в основании.
    """
    items = list(envelopes)
    keys = public_keys or {}
    basis = "conclusions and refusals carrying an envelope whose id recomputes and whose signature verifies"
    if not items:
        return not_measured("evidence_coverage", basis=basis, reason="no conclusions were recorded")
    verified = 0
    for item in items:
        issuer = item.get("provider", {}).get("id") if isinstance(item, dict) else None
        if envelope_is_evidence(item, keys.get(str(issuer))):
            verified += 1
    return measured("evidence_coverage", verified / len(items), unit="share", basis=basis)


def refusal_code_completeness(refusals: Iterable[dict]) -> Metric:
    """Доля отказов, у которых код из закрытого перечня, а не свободный текст."""
    items = list(refusals)
    basis = "refusals carrying a code from a closed list"
    if not items:
        return not_measured("refusal_code_completeness", basis=basis, reason="no refusals were recorded")
    with_code = 0
    for item in items:
        code = item.get("code") if isinstance(item, dict) else None
        if code in KNOWN_REFUSAL_CODES:
            with_code += 1
    return measured("refusal_code_completeness", with_code / len(items), unit="share", basis=basis)


def closed_intents_share(intents: Iterable[dict]) -> Metric:
    """Доля намерений, закрытых находкой: закрытые к поданным.

    Сравнивать эту долю с чужой воронкой можно только на живых данных: синтетика для этого
    не годится, и величина об этом говорит прямо.
    """
    items = list(intents)
    basis = "intents closed by a find over intents submitted"
    if not items:
        return not_measured("closed_intents_share", basis=basis, reason="no intents were recorded")
    closed = sum(1 for item in items if item.get("closed"))
    return measured("closed_intents_share", closed / len(items), unit="share", basis=basis)


def matches_above_floor(matches: Iterable[dict]) -> Metric:
    """Доля находок, прошедших объявленный порог. Ниже порога находки не возвращаются."""
    items = list(matches)
    basis = "finds that passed the declared floor over finds returned"
    if not items:
        return not_measured("matches_above_floor", basis=basis, reason="no matches were recorded")
    passed = sum(1 for item in items if item.get("passed_floor"))
    return measured("matches_above_floor", passed / len(items), unit="share", basis=basis)


def time_to_decision_median(decisions: Iterable[dict]) -> Metric:
    """Медиана времени от находки до решения человека, в секундах."""
    items = list(decisions)
    basis = "median seconds from find to the human decision"
    stamps = [item.get("seconds_to_decision") for item in items if isinstance(item, dict)]
    stamps = [value for value in stamps if isinstance(value, (int, float)) and value >= 0]
    if not stamps:
        return not_measured(
            "time_to_decision_median", basis=basis, reason="no decisions with timestamps were recorded"
        )
    return measured("time_to_decision_median", float(statistics.median(stamps)), unit="seconds", basis=basis)


def false_positive_share(labels: Iterable[dict]) -> Metric:
    """Доля находок, помеченных человеком как мусорные. Без пометок величина неизмерена."""
    items = list(labels)
    basis = "finds labelled junk by the person over labelled finds"
    if not items:
        return not_measured(
            "false_positive_share",
            basis=basis,
            reason="no human labels were recorded; the instrument does not guess them",
        )
    junk = sum(1 for item in items if item.get("junk"))
    return measured("false_positive_share", junk / len(items), unit="share", basis=basis)


def network_health(participants: Iterable[dict]) -> Metric:
    """Доля участников с завершённым намерением на конец наблюдения."""
    items = list(participants)
    basis = "participants with a completed intent at the end of the observation"
    if not items:
        return not_measured("network_health", basis=basis, reason="no participants were recorded")
    complete = sum(1 for item in items if item.get("completed"))
    return measured("network_health", complete / len(items), unit="share", basis=basis)


# --- сборка отчёта ---

def compute_all(record: dict[str, Any]) -> list[Metric]:
    """Посчитать все величины из одной записи наблюдения."""
    keys = {
        str(name): pem if isinstance(pem, bytes) else str(pem).encode()
        for name, pem in (record.get("public_keys") or {}).items()
    }
    return [
        evidence_coverage(record.get("envelopes", []), keys),
        refusal_code_completeness(record.get("refusals", [])),
        closed_intents_share(record.get("intents", [])),
        matches_above_floor(record.get("matches", [])),
        time_to_decision_median(record.get("decisions", [])),
        false_positive_share(record.get("labels", [])),
        network_health(record.get("participants", [])),
    ]


@dataclass
class MeasureReport:
    metrics: list[Metric] = field(default_factory=list)
    source: str = ""

    @property
    def measured_count(self) -> int:
        return sum(1 for metric in self.metrics if metric.state == MEASURED)

    @property
    def unmeasured(self) -> list[Metric]:
        return [metric for metric in self.metrics if metric.state == NOT_MEASURED]

    def to_json(self) -> dict:
        return {
            "source": self.source,
            "measured": self.measured_count,
            "not_measured": len(self.unmeasured),
            "metrics": [metric.to_json() for metric in self.metrics],
        }

    def as_table(self) -> str:
        lines = []
        for metric in self.metrics:
            if metric.state == MEASURED:
                lines.append(f"{metric.name}: {metric.value:.4f} {metric.unit}")
            else:
                lines.append(f"{metric.name}: not measured ({metric.reason})")
        return "\n".join(lines)


def measure(record: dict[str, Any], *, source: str = "") -> MeasureReport:
    return MeasureReport(metrics=compute_all(record), source=source)
