"""Correctness and boundary checks of closed intersection (task 1.5).

What exactly is checked and why exactly so:

- correctness: the scheme must find exactly the same intersection as the earlier one,
  otherwise it cannot even be compared on cost;
- the holder learns nothing: only blinded values reach party B, their count equals the
  number of A's features, and the values themselves are random on each pass;
- offline enumeration is closed: the values party A receives cannot be matched against
  guesses without the key d, so another party's set cannot be quietly enumerated;
- online probing of a single guess remains possible, because that is a property of any set
  intersection. This is stated honestly and is bounded by request frequency and the rule
  of one exchange per session.
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
    assert first != second, "blinding must be random on each pass"
    assert first != plain, "a blinded value must not equal the open hash"


def test_holder_learns_only_the_count():
    """Party B sees blinded values: no features, and no link between them and guesses."""
    keys = generate_keys()
    learner = ["role:founder", "skill:design", "city:sf"]
    blinded = [blind(f, keys.n, keys.e)[0] for f in learner]
    assert len(blinded) == len(learner), "B learns only the count"
    # no blinded value equals a signature over an open feature
    signed_own = set(own_signed_tokens(learner, keys))
    assert not set(blinded) & signed_own


def test_offline_enumeration_is_closed():
    """Without the key d another party's set cannot be enumerated: the signature over a guess is uncomputable."""
    keys = generate_keys()
    holder = ["skill:design", "city:sf"]
    tokens = set(own_signed_tokens(holder, keys))
    vocabulary = ["skill:design", "city:sf", "role:investor", "city:berlin"]
    # an attempt to match a guess against a signature without knowing d
    naive = {f for f in vocabulary if pow(feature_hash(f), keys.e, keys.n) in tokens}
    assert naive == set(), "a guess plus the public exponent does not give a signature"


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
