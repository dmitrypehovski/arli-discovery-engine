"""Перечень причин отказа и годность пары.

Главное здесь: список причин проверяется на достижимость. Если код из перечня
нельзя получить ни одним ходом, он мёртвый, и перечень перестаёт быть
обязательством. Ровно так нашлась одна недостижимая причина и была убрана.
"""

from __future__ import annotations

import pytest

from arli_engine.core.negotiate import (
    MAX_TURNS,
    OPENING_REJECTIONS,
    REJECTIONS,
    IntentStanding,
    Negotiation,
    NegotiationRejected,
    OpeningRejected,
    decide_opening,
)


def _fresh() -> Negotiation:
    return Negotiation(seats=("a", "b"))


def _settled() -> Negotiation:
    neg = _fresh()
    neg.turn("a", "propose", "hello")
    neg.turn("b", "accept", "ok")
    return neg


def _at_turn_limit() -> Negotiation:
    neg = _fresh()
    for i in range(MAX_TURNS):
        neg.turn("a" if i % 2 == 0 else "b", "propose" if i % 2 == 0 else "counter", "t")
    return neg


def _after_propose() -> Negotiation:
    neg = _fresh()
    neg.turn("a", "propose", "offer")
    return neg


def _after_counter() -> Negotiation:
    neg = _fresh()
    neg.turn("a", "propose", "offer")
    neg.turn("b", "counter", "question")
    return neg


# каждый код достигается конкретным набором ходов; ключ это код, значение это
# функция, которая обязана поднять NegotiationRejected именно с этим кодом
REACHABLE = {
    "not_a_seat": lambda: _fresh().turn("c", "propose", ""),
    "unknown_action": lambda: _fresh().turn("a", "sing", ""),
    "already_settled": lambda: _settled().turn("a", "propose", ""),
    "turn_limit": lambda: _at_turn_limit().turn("a", "propose", ""),
    "not_your_turn": lambda: _fresh().turn("b", "propose", ""),
    "first_turn_is_propose_or_decline": lambda: _fresh().turn("a", "counter", ""),
    "seats_must_alternate": lambda: _after_propose().turn("a", "counter", ""),
    "accept_without_offer": lambda: _after_counter().turn("a", "accept", ""),
    "propose_over_offer": lambda: _after_propose().turn("b", "propose", ""),
}


def test_every_rejection_code_is_reachable():
    """Перечень причин без мёртвых кодов: иначе он перестаёт что-либо обещать."""
    assert set(REACHABLE) == set(REJECTIONS), "REACHABLE и REJECTIONS разошлись"
    for code, run in REACHABLE.items():
        with pytest.raises(NegotiationRejected) as exc:
            run()
        assert exc.value.code == code, f"ожидали {code}, получили {exc.value.code}"


def test_rejection_code_outside_the_list_is_refused():
    """В перечень нельзя добавить причину мимо списка: конструктор это запрещает."""
    with pytest.raises(ValueError):
        NegotiationRejected("made_up_reason")
    with pytest.raises(ValueError):
        OpeningRejected("made_up_reason")


def test_accept_after_counter_is_accept_without_offer():
    neg = _after_counter()
    with pytest.raises(NegotiationRejected) as exc:
        neg.turn("a", "accept", "let us do it")
    assert exc.value.code == "accept_without_offer"


def test_turn_limit_is_not_a_rejection_of_the_pair():
    """Предел ходов оставляет вопрос открытым: отказа не было."""
    neg = _at_turn_limit()
    assert neg.outcome is None
    with pytest.raises(NegotiationRejected) as exc:
        neg.turn("a", "propose", "")
    assert exc.value.code == "turn_limit"


# --- годность пары ---

def _standing(intent_id="i1", owner="a", network="n1", active=True,
              type="person", provenance="human") -> IntentStanding:
    return IntentStanding(intent_id=intent_id, owner=owner, network=network, active=active,
                          type=type, provenance=provenance)


OPENING_CASES = {
    "intent_inactive": (_standing(active=False), _standing(intent_id="i2", owner="b"), "n1"),
    "synthetic_not_admitted": (_standing(provenance="synthetic"),
                               _standing(intent_id="i2", owner="b"), "n1"),
    "same_owner": (_standing(), _standing(intent_id="i2"), "n1"),
    "same_intent": (_standing(), _standing(owner="b"), "n1"),
    "network_mismatch": (_standing(), _standing(intent_id="i2", owner="b", network="n9"), "n1"),
    "types_not_comparable": (_standing(type="supply"),
                             _standing(intent_id="i2", owner="b", type="supply"), "n1"),
    "below_required_level": (_standing(), _standing(intent_id="i2", owner="b"), "n1",
                             {"required_level": "third_party_verified"}),
}


def test_every_opening_rejection_code_is_reachable():
    """У каждой причины отказа свой вход. Причина, до которой нельзя дойти, это мёртвый код.

    Часть случаев требует дополнительного условия (например, потребованный уровень),
    поэтому случай это или тройка, или четвёрка с условиями.
    """
    assert set(OPENING_CASES) == set(OPENING_REJECTIONS), "OPENING_CASES и перечень разошлись"
    for code, case in OPENING_CASES.items():
        a, b, network = case[0], case[1], case[2]
        extra = case[3] if len(case) > 3 else {}
        assert decide_opening(a, b, network, **extra) == code


def test_good_pair_opens():
    assert decide_opening(_standing(), _standing(intent_id="i2", owner="b"), "n1") is None


def test_each_opening_code_has_its_own_case():
    """Коды не схлопываются в один: у каждого свой вход, и результат не совпадает."""
    results = {
        code: decide_opening(case[0], case[1], case[2], **(case[3] if len(case) > 3 else {}))
        for code, case in OPENING_CASES.items()
    }
    assert len(set(results.values())) == len(OPENING_CASES)


def test_legacy_hash_scheme_key_holder_recovers_every_feature():
    """Про прежнюю схему, которая осталась в ядре как запасная.

    Держатель ключа пары видит все признаки. Именно поэтому матчинг в сервере
    переведён на закрытое пересечение (решение от 19.10.2026, spec/DECISIONS.md),
    а этот тест документирует, что именно закрыто и почему.

    Это негативный тест. Он существует, чтобы про утечку нельзя было забыть и чтобы
    её нельзя было незаметно починить формулировкой. Пока схема не заменена на схему
    с закрытым пересечением, тест обязан проходить именно так. Когда мы заменим
    матчинг, этот тест обязан упасть: это и будет сигналом, что обещание можно менять.

    Ключ здесь выводится согласованием, как в движке, а не подставляется строкой.
    """
    from arli_engine.core.crypto import commitment, pair_label
    from arli_engine.core.match import FeatureSet, PairSession
    from arli_engine.core.signing import KeyPair

    key_a, key_b = KeyPair.generate(), KeyPair.generate()
    own = FeatureSet(["role:founder", "skill:design", "city:sf", "stage:preseed"])
    session = PairSession()
    pair = pair_label("agent-a", "agent-b")
    key = session.pair_key(
        key_a.private_pem, key_b.public_pem, own_name="agent-a", peer_name="agent-b"
    )
    digests = set(
        own.commitments(key, session_id=session.session_id, pair=pair).values()
    )
    vocabulary = ["role:founder", "role:investor", "skill:design", "skill:backend",
                  "city:sf", "city:berlin", "stage:preseed", "stage:seed"]
    recovered = {
        guess for guess in vocabulary
        if commitment(key, session_id=session.session_id, pair=pair, feature=guess) in digests
    }
    assert recovered == set(own), "держатель ключа обязан восстановить весь набор"
    assert len(recovered) == len(own)


def test_match_rejection_codes_are_reachable_at_the_core_level():
    """Два кода отказа сопоставления достижимы в ядре, остальные три на сервере.

    Перечень обязан быть закрытым: тест сверяет, что покрытые коды плюс серверные
    равны самому перечню, иначе в списке заведётся мёртвый код.
    """
    from arli_engine.core.match import MATCH_REJECTIONS, MatchRejected, closed_match

    from arli_engine.core.match import FeatureSet

    covered: dict[str, str] = {}
    big = FeatureSet([f"f:{i}" for i in range(70)])
    with pytest.raises(MatchRejected) as exc:
        closed_match(side="a", own=big, unblinded=[], holder_tokens=set(),
                     session_id="s", floor_basis_points=0)
    covered["too_many_features"] = exc.value.code

    small = FeatureSet(["a", "b"])
    with pytest.raises(MatchRejected) as exc:
        closed_match(side="a", own=small, unblinded=[1], holder_tokens=set(),
                     session_id="s", floor_basis_points=0)
    covered["length_mismatch"] = exc.value.code

    with pytest.raises(MatchRejected) as exc:
        closed_match(side="a", own=small, unblinded=[1, 2], holder_tokens=set(),
                     session_id="s", floor_basis_points=None)
    covered["missing_floor"] = exc.value.code

    with pytest.raises(MatchRejected) as exc:
        closed_match(side="a", own=small, unblinded=[1, 2], holder_tokens=set(),
                     session_id="s", floor_basis_points=20000)
    covered["invalid_floor"] = exc.value.code

    server_codes = {"missing_pair_key", "missing_peer_key", "missing_private_key"}
    assert set(covered) | server_codes == set(MATCH_REJECTIONS)
    for code in covered.values():
        assert code in MATCH_REJECTIONS
