"""Матчинг без третьего держателя.

Что было у Index Network: чтобы понять, совпадают ли намерения двух людей,
сеть обязана прочитать оба намерения. Здесь наоборот.

Стороны переводят свои признаки в отпечатки и сравнивают их. Ключ пары не
передаётся, а выводится согласованием на кривой (см. core/crypto.py), отпечаток
привязан к сессии и стороне. Каждая сторона узнаёт только то, какие признаки
совпали у неё самой.

Честные ограничения записаны в LIMITATIONS и попадают в конверт:
- третья сторона признаков не видит совсем: она не знает ключа пары;
- противная сторона их видит, потому что обязана считать свои отпечатки тем же
  ключом, и может перебрать словарь признаков: это свойство любого сравнения
  по хешам без закрытого пересечения;
- признаков мало, поэтому пересечение это сигнал, а не доказательство.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field

from arli_engine.core.crypto import commitment, derive_pair_key, pair_label
from arli_engine.core.evidence import Completeness, Evidence
from arli_engine.core.psi import MAX_FEATURES_PER_EXCHANGE, matched_features

LIMITATIONS = [
    "A counterparty can test guesses about your features; equal cost of both sides is inherent to hash intersection.",
    # Прежняя формулировка была перевёрнута: грубый словарь признаков делает набор
    # догадок маленьким, то есть подбор проще, а не сложнее. Признаки грубы ради
    # смысла совпадения, и это ровно то, что снижает цену подбора.
    "Feature vocabulary is coarse on purpose, which makes guessing cheaper, not harder; a party that holds the pair key can recover your features from their commitments.",
    "No third party sees both feature sets: the pair key is derived, never transmitted, and matching is computed locally on each side.",
    "The counterparty is not a third party: it derives the same key and can enumerate your features until closed intersection is implemented.",
    "Commitments are bound to the session and to the pair, so they cannot be replayed in another session.",
    "Overlap is a signal, not a proof of fit; the negotiation step exists for that.",
]


def normalize_feature(text: str) -> str:
    """Привести признак к сравнимому виду."""
    return " ".join(text.strip().lower().split())


class FeatureSet:
    """Набор признаков одной стороны."""

    def __init__(self, features: list[str]) -> None:
        cleaned = {normalize_feature(f) for f in features if normalize_feature(f)}
        if not cleaned:
            raise ValueError("a feature set cannot be empty")
        self._features = sorted(cleaned)

    def __len__(self) -> int:
        return len(self._features)

    def __iter__(self):
        return iter(self._features)

    def commitments(self, pair_key: str, *, session_id: str, pair: str) -> dict[str, str]:
        """Отпечатки признаков под ключом пары, привязанные к сессии и к паре.

        Ключ приходит готовым: он выведен согласованием (core/crypto.py) и по
        проводу не ходит. Метка пары одна и та же у обеих сторон, поэтому один
        и тот же признак даёт один и тот же отпечаток, а отпечаток из другой
        сессии не совпадёт.
        """
        return {
            f: commitment(pair_key, session_id=session_id, pair=pair, feature=f)
            for f in self._features
        }


@dataclass
class MatchOutcome:
    """Результат локального сравнения на одной стороне.

    Доля совпадения выражается целыми базисными пунктами: дробные числа
    запрещены в каноническом виде доказательства, иначе идентификатор
    конверта перестаёт воспроизводиться.
    """

    matched_features: list[str]
    overlap_basis_points: int
    side: str
    evidence: Evidence

    @property
    def overlap_ratio_text(self) -> str:
        total = self.evidence.parameters["own_feature_count"]
        return f"{len(self.matched_features)}/{total}"

    def to_json(self) -> dict:
        return {
            "side": self.side,
            "matched_features": self.matched_features,
            "overlap_basis_points": self.overlap_basis_points,
            "overlap_ratio": self.overlap_ratio_text,
            "evidence": self.evidence.to_json(),
        }


@dataclass
class PairSession:
    """Сессия пары: её идентификатор входит в вывод ключа и в каждый отпечаток.

    Ключ по проводу не ходит: обе стороны выводят его сами из своих ключей
    подписи и общего идентификатора сессии.
    """

    session_id: str = field(default_factory=lambda: secrets.token_hex(8))
    disclosed: set[str] = field(default_factory=set)

    def pair_key(
        self,
        own_private_pem: bytes,
        peer_public_pem: bytes,
        *,
        own_name: str,
        peer_name: str,
    ) -> str:
        """Вывести ключ пары для этой сессии."""
        return derive_pair_key(
            own_private_pem,
            peer_public_pem,
            session_id=self.session_id,
            agent_a=own_name,
            agent_b=peer_name,
        )

    def commitments(
        self,
        features: FeatureSet,
        own_private_pem: bytes,
        peer_public_pem: bytes,
        *,
        own_name: str,
        peer_name: str,
    ) -> dict[str, str]:
        """Отпечатки своих признаков для этой сессии."""
        key = self.pair_key(
            own_private_pem, peer_public_pem, own_name=own_name, peer_name=peer_name
        )
        return features.commitments(
            key, session_id=self.session_id, pair=pair_label(own_name, peer_name)
        )

    def disclose(self, feature: str) -> None:
        self.disclosed.add(normalize_feature(feature))


class MatchRejected(RuntimeError):
    """Отказ сопоставления с кодом из закрытого перечня MATCH_REJECTIONS."""

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in MATCH_REJECTIONS:
            raise ValueError(f"{code!r} is not in MATCH_REJECTIONS")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# Перечень причин отказа сопоставления. Закрытый список: попытка либо проходит,
# либо получает один из этих кодов. Тест проверяет достижимость каждого.
MATCH_REJECTIONS = (
    "too_many_features",
    "missing_pair_key",
    "missing_peer_key",
    "missing_private_key",
    "length_mismatch",
    "missing_floor",
    "invalid_floor",
)

# Границы порога: доля совпадения выражается базисными пунктами, 0 это нет порога,
# 10000 это полное совпадение.
FLOOR_MIN = 0
FLOOR_MAX = 10000

# Ограничения закрытого пересечения. Здесь важно не льстить схеме: офлайн-перебор
# закрыт, но проверить одну догадку онлайн можно, и это сказано прямым текстом.
CLOSED_LIMITATIONS = [
    "Offline enumeration of the counterparty's features is closed: the learner receives a signature over a feature and cannot match it against guesses without the holder's private key.",
    "Online probing of a single guess remains possible, because any set intersection reveals the result for every element the learner submits; it is bounded by one exchange per session and by the feature cap.",
    "The holder learns how many features were submitted, not their values.",
    "A chosen feature count cap bounds the cost of the exchange; above it matching is refused, not truncated.",
    "Overlap is a signal, not a proof of fit; the negotiation step exists for that.",
]


def closed_match(
    *,
    side: str,
    own: FeatureSet,
    unblinded: list[int],
    holder_tokens: set[int],
    session_id: str,
    floor_basis_points: int,
    policy_filtered: list[tuple[str, str, int | None]] | None = None,
) -> MatchOutcome:
    """Сопоставление закрытым пересечением (слепое подписывание RSA).

    Сторона узнаёт только совпадения по своим признакам. Значения, полученные от
    держателя, это подписи над его признаками, поэтому перебрать чужой набор
    офлайн нельзя.

    Порог обязателен и объявляется вызывающим: без него нет способа отличить
    хорошее совпадение от заполненного места. Ниже порога находка не возвращается
    вовсе, а не добивается до какого-то числа.
    """
    if floor_basis_points is None:
        raise MatchRejected("missing_floor", "floor_basis_points is required")
    if not isinstance(floor_basis_points, int) or not (FLOOR_MIN <= floor_basis_points <= FLOOR_MAX):
        raise MatchRejected(
            "invalid_floor", f"floor must be an integer within {FLOOR_MIN}..{FLOOR_MAX}"
        )
    if len(own) > MAX_FEATURES_PER_EXCHANGE:
        raise MatchRejected("too_many_features", f"{len(own)} features")
    if len(unblinded) != len(own):
        raise MatchRejected(
            "length_mismatch", f"{len(unblinded)} signed values for {len(own)} features"
        )

    features = list(own)
    matched = matched_features(features, unblinded, holder_tokens)
    overlap_basis_points = round(10000 * len(matched) / len(own))
    passed_floor = overlap_basis_points >= floor_basis_points

    completeness = Completeness()
    completeness.exclude("own_unmatched_features", "never_returned_by_design")
    completeness.exclude("counterpart_features", "only_signatures_over_their_features_are_received")
    completeness.exclude(
        "counterpart_feature_count", "not_disclosed_by_us_either: the applicant count is theirs"
    )
    if not passed_floor:
        # Находка ниже порога не отдаётся и это записано, а не спрятано.
        completeness.exclude("below_floor", "not_returned: overlap is under the declared floor")
    for section, reason, count in policy_filtered or []:
        completeness.exclude(section, reason, count)

    returned = matched if passed_floor else []

    evidence = Evidence(
        provider={
            "id": "arli-closed-intersection",
            "name": "ARLI closed set intersection (blind RSA)",
            "version": "1",
        },
        operation="closed_intersection",
        parameters={
            "session_id": session_id,
            "side": side,
            "own_feature_count": len(own),
            "scheme": "closed_blind_rsa",
            "feature_cap": MAX_FEATURES_PER_EXCHANGE,
            "floor_basis_points": floor_basis_points,
            "passed_floor": passed_floor,
        },
        normalized_result={
            "matched_feature_count": len(returned),
            "overlap_basis_points": overlap_basis_points,
            "matched_features": returned,
        },
        confidence="derived",
        authority="local-calculation",
        limitations=list(CLOSED_LIMITATIONS),
        completeness=completeness,
    )
    return MatchOutcome(
        matched_features=returned,
        overlap_basis_points=overlap_basis_points,
        side=side,
        evidence=evidence,
    )


def closed_match_holder(
    *,
    side: str,
    own: FeatureSet,
    matched: list[str],
    session_id: str,
    floor_basis_points: int,
) -> MatchOutcome:
    """Конверт держателя закрытого пересечения.

    Держатель считает своё пересечение по именам, которые нашёл заявитель, и
    записывает то, что он узнал: совпадения, число присланных признаков и порог,
    о котором стороны договорились.
    """
    names = set(matched)
    found = sorted(f for f in own if f in names)
    overlap_basis_points = round(10000 * len(found) / len(own))

    completeness = Completeness()
    completeness.exclude("applicant_features", "only_blinded_values_were_received")
    completeness.exclude("own_unmatched_features", "never_returned_by_design")

    evidence = Evidence(
        provider={
            "id": "arli-closed-intersection-holder",
            "name": "ARLI closed set intersection, holder side (blind RSA)",
            "version": "1",
        },
        operation="closed_intersection_holder",
        parameters={
            "session_id": session_id,
            "side": side,
            "own_feature_count": len(own),
            "scheme": "closed_blind_rsa",
            "floor_basis_points": floor_basis_points,
        },
        normalized_result={
            "matched_feature_count": len(found),
            "overlap_basis_points": overlap_basis_points,
            "matched_features": found,
        },
        confidence="derived",
        authority="local-calculation",
        limitations=list(CLOSED_LIMITATIONS),
        completeness=completeness,
    )
    return MatchOutcome(
        matched_features=found,
        overlap_basis_points=overlap_basis_points,
        side=side,
        evidence=evidence,
    )


def local_match(
    *,
    side: str,
    own: FeatureSet,
    own_commitments: dict[str, str],
    counterpart_commitments: list[str],
    session_id: str,
    policy_filtered: list[tuple[str, str, int | None]] | None = None,
) -> MatchOutcome:
    """Найти совпадения, зная только свои признаки и чужие отпечатки."""
    foreign = set(counterpart_commitments)
    matched = sorted(f for f, digest in own_commitments.items() if digest in foreign)
    overlap_basis_points = round(10000 * len(matched) / len(own))

    completeness = Completeness()
    # Наш матчер по построению не возвращает несовпавшие признаки ни свои,
    # ни чужие. Это фильтр, и он обязан быть виден в конверте.
    completeness.exclude("own_unmatched_features", "never_returned_by_design")
    completeness.exclude("counterpart_features", "only_bound_commitments_are_received")
    # Число чужих признаков больше не попадает ни в конверт, ни в ответ: это была
    # отдельная утечка (T5), и она закрыта вместе с привязкой отпечатков.
    completeness.exclude(
        "counterpart_feature_count", "not_disclosed: the count itself leaks the size of their set"
    )
    for section, reason, count in policy_filtered or []:
        completeness.exclude(section, reason, count)

    evidence = Evidence(
        provider={
            "id": "arli-local-matcher",
            "name": "ARLI local commitment intersection",
            "version": "1",
        },
        operation="local_feature_overlap",
        parameters={
            "session_id": session_id,
            "side": side,
            "own_feature_count": len(own),
        },
        normalized_result={
            "matched_feature_count": len(matched),
            "overlap_basis_points": overlap_basis_points,
            "matched_features": matched,
        },
        confidence="derived",
        authority="local-calculation",
        limitations=list(LIMITATIONS),
        completeness=completeness,
    )
    return MatchOutcome(
        matched_features=matched,
        overlap_basis_points=overlap_basis_points,
        side=side,
        evidence=evidence,
    )
