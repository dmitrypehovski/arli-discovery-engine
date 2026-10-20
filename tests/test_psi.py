"""Проверки корректности и границ закрытого пересечения (задача 1.5).

Что именно проверяется и почему именно так:

- корректность: схема обязана находить ровно то же пересечение, что и прежняя,
  иначе её нельзя даже сравнивать по цене;
- держатель ничего не узнаёт: до стороны B доходят только ослеплённые значения,
  их количество совпадает с числом признаков A, а сами значения случайны при
  каждом проходе;
- офлайн-перебор закрыт: полученные стороной A значения нельзя сопоставить с
  догадками без ключа d, поэтому тихо перебрать чужой набор нельзя;
- онлайн-проверка догадки остаётся возможной, потому что это свойство любого
  пересечения по множествам. Это записано честно и закрывается частотой
  обращений и правилом одного обмена на сессию.
"""

from __future__ import annotations

from arli_engine.core.psi import (
    Keys,
    blind,
    feature_hash,
    generate_keys,
    own_signed_tokens,
    set_intersection,
    sign,
    unblind,
)


def test_closed_intersection_matches_the_plain_one():
    keys = generate_keys()
    learner = ["role:founder", "skill:design", "city:sf", "stage:preseed"]
    holder = ["skill:design", "city:sf", "role:investor"]
    found = set_intersection(keys.n, keys.e, keys.d, learner, holder)
    assert found == sorted(set(learner) & set(holder))
    assert found == ["city:sf", "skill:design"]


def test_closed_intersection_is_empty_for_disjoint_sets():
    keys = generate_keys()
    assert set_intersection(keys.n, keys.e, keys.d, ["a:x"], ["b:y"]) == []


def test_blinding_hides_the_value_and_is_random_each_time():
    keys = generate_keys()
    first, _ = blind("skill:design", keys.n, keys.e)
    second, _ = blind("skill:design", keys.n, keys.e)
    plain = feature_hash("skill:design")
    assert first != second, "ослепление обязано быть случайным при каждом проходе"
    assert first != plain, "ослеплённое значение не должно совпадать с открытым хешем"


def test_holder_learns_only_the_count():
    """Сторона B видит ослеплённые значения: ни признаков, ни их связи с догадками."""
    keys = generate_keys()
    learner = ["role:founder", "skill:design", "city:sf"]
    blinded = [blind(f, keys.n, keys.e)[0] for f in learner]
    assert len(blinded) == len(learner), "B узнаёт только количество"
    # ни одно ослеплённое значение не равно подписи над открытым признаком
    signed_own = set(own_signed_tokens(learner, keys))
    assert not set(blinded) & signed_own


def test_offline_enumeration_is_closed():
    """Без ключа d чужой набор не перебрать: подпись над догадкой невычислима."""
    keys = generate_keys()
    holder = ["skill:design", "city:sf"]
    tokens = set(own_signed_tokens(holder, keys))
    vocabulary = ["skill:design", "city:sf", "role:investor", "city:berlin"]
    # попытка сопоставить догадку с подписью без знания d
    naive = {f for f in vocabulary if pow(feature_hash(f), keys.e, keys.n) in tokens}
    assert naive == set(), "догадка плюс открытый показатель не даёт подписи"


def test_unblinding_returns_the_signed_feature():
    keys = generate_keys()
    for feature in ("role:founder", "skill:design"):
        blinded, factor = blind(feature, keys.n, keys.e)
        unblinded = unblind(sign(blinded, keys.d, keys.n), factor, keys.n)
        assert unblinded in set(own_signed_tokens([feature], keys))


def test_keys_dataclass_keeps_the_private_exponent():
    keys: Keys = generate_keys()
    assert keys.n.bit_length() >= 2048
    assert keys.public() == (keys.n, keys.e)
    assert keys.d != keys.e
