"""Closed intersection: an experimental prototype for task 1.5 of the plan.

The scheme: blind RSA signing (one-sided PSI). Party B holds the key (N, e, d),
party A sends its features blinded, B signs them with its key and returns the
signed values together with its own signature over its own features. A unblinds
and compares. Properties:

- A learns only the intersection: the signature over another party's feature is
  H(feature)^d, and without d the dictionary cannot be enumerated, which closes
  our T1;
- B learns nothing about A's features: only blinded values reach it, and it sees
  just their count;
- both sides obtain the same intersection.

The module is marked experimental: it is NOT wired into matching. Matching switches
to it only after the founder's decision based on the measurement (step 3 of task 1.5).

A caveat for reading the measurement numbers: the exponentiation is done by Python's
C implementation of big numbers, so this is an honest estimate for a big-number
scheme, but not for an implementation on a curve through OpenSSL, where point
multiplication is cheaper.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from math import gcd
from secrets import randbelow

from cryptography.hazmat.primitives.asymmetric import rsa

MODULUS_BYTES = 256  # 2048 bits
MAX_FEATURES_PER_EXCHANGE = 64  # cap from the measurement: beyond it the cost grows linearly


def feature_hash(feature: str) -> int:
    """A feature as a number modulo: the same on both sides.

    The hash is taken over the canonical token, not the string as typed, so a need and an offer of the
    same commodity meet. The rule is published (see core/vocabulary.py) and identical in every
    implementation, which is what lets both sides agree without swapping lists.
    """
    from arli_engine.core.vocabulary import canonical_token

    return int.from_bytes(hashlib.sha256(canonical_token(feature).encode("utf-8")).digest(), "big")


def blind_set(features: list[str], n: int, e: int) -> tuple[list[int], list[int]]:
    """Blind a set of features. Returns the values and the factors for unblinding."""
    if len(features) > MAX_FEATURES_PER_EXCHANGE:
        raise ValueError("too_many_features")
    pairs = [blind(f, n, e) for f in features]
    return [value for value, _ in pairs], [factor for _, factor in pairs]


def unblind_set(signed: list[int], factors: list[int], n: int) -> list[int]:
    """Unblind the signed values."""
    if len(signed) != len(factors):
        raise ValueError("signed values and factors must have the same length")
    return [unblind(value, factor, n) for value, factor in zip(signed, factors)]


def matched_features(
    features: list[str], unblinded: list[int], holder_tokens: set[int]
) -> list[str]:
    """Which of our own features were found at the holder: signatures are compared as values."""
    if len(features) != len(unblinded):
        raise ValueError("features and unblinded values must have the same length")
    return sorted(
        feature for feature, token in zip(features, unblinded) if token in holder_tokens
    )


@dataclass(frozen=True)
class Keys:
    """The signing party's key: N and e are public, d stays with B."""

    n: int
    e: int
    d: int

    def public(self) -> tuple[int, int]:
        return self.n, self.e


def generate_keys() -> Keys:
    key = rsa.generate_private_key(public_exponent=65537, key_size=MODULUS_BYTES * 8)
    numbers = key.private_numbers()
    return Keys(n=numbers.public_numbers.n, e=numbers.public_numbers.e, d=numbers.d)


DEFAULT_EPOCH_SECONDS = 3600


@dataclass
class EpochKey:
    """One key for an epoch, with the holder's own tokens computed once.

    Why an epoch and not a key per exchange. Generating an RSA key costs more than the exchange itself, and
    the holder was recomputing tokens over its own list for every seeker. Within an epoch the key is made
    once and the tokens are computed once; the remaining per-exchange work is signing the seeker's blinded
    values, which nobody else can do for us.

    What this costs, stated plainly: tokens are stable within an epoch, so two seekers that compare notes
    can tell that they hold the same item. They could already tell that by intersecting their own lists, but
    the text has to say it rather than leave it to be discovered. An epoch must expire, and a revoked agent's
    epoch must stop being served.
    """

    epoch_id: str
    keys: "Keys"
    created_at: float
    ttl_seconds: int = DEFAULT_EPOCH_SECONDS
    _tokens: dict[str, int] = field(default_factory=dict)

    def expired(self, now: float) -> bool:
        return (now - self.created_at) >= self.ttl_seconds

    def tokens_for(self, features: list[str]) -> list[int]:
        """Signed tokens over our own list, computed once per epoch and then reused."""
        missing = [feature for feature in features if feature not in self._tokens]
        if missing:
            for feature in missing:
                self._tokens[feature] = sign(feature_hash(feature), self.keys.d, self.keys.n)
        return [self._tokens[feature] for feature in features]


def new_epoch(ttl_seconds: int = DEFAULT_EPOCH_SECONDS) -> EpochKey:
    import secrets as _secrets
    import time as _time

    return EpochKey(epoch_id=f"ep-{_secrets.token_hex(6)}", keys=generate_keys(),
                    created_at=_time.time(), ttl_seconds=ttl_seconds)


def _blind_factor(n: int) -> int:
    while True:
        r = randbelow(n - 3) + 2
        if gcd(r, n) == 1:
            return r


def blind(feature: str, n: int, e: int) -> tuple[int, int]:
    """Blind a feature: (value, factor) for later unblinding."""
    r = _blind_factor(n)
    blinded = feature_hash(feature) * pow(r, e, n) % n
    return blinded, r


def sign(blinded: int, d: int, n: int) -> int:
    """Sign the blinded value with party B's key."""
    return pow(blinded, d, n)


def unblind(signed: int, r: int, n: int) -> int:
    return signed * pow(r, -1, n) % n


def own_signed_tokens(features: list[str], keys: Keys) -> list[int]:
    """Our own features in signed form: what party A compares against."""
    return [pow(feature_hash(f), keys.d, keys.n) for f in features]


def set_intersection(n: int, e: int, d: int, learner: list[str], holder: list[str]) -> list[str]:
    """A full pass of the scheme, returning the learner's features found at the holder.

    Here both sides are in one process: this is needed to check correctness. In the
    measurement the same steps are counted separately, to honestly measure each side.
    """
    blinded = {f: blind(f, n, e) for f in learner}
    signed_back = {f: sign(b, d, n) for f, (b, _) in blinded.items()}
    unblinded = {f: unblind(s, blinded[f][1], n) for f, s in signed_back.items()}
    holder_tokens = set(own_signed_tokens(holder, Keys(n=n, e=e, d=d)))
    return sorted(f for f, token in unblinded.items() if token in holder_tokens)
