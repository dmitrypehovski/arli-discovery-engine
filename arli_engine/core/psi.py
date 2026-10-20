"""Закрытое пересечение: экспериментальный прототип для задачи 1.5 плана.

Схема: слепое подписывание RSA (one-sided PSI). Сторона B держит ключ (N, e, d),
сторона A присылает свои признаки ослеплёнными, B подписывает их своим ключом и
возвращает подписанное вместе со своей подписью над своими признаками. A снимает
ослепление и сравнивает. Отсюда свойства:

- A узнаёт только пересечение: подпись над чужим признаком это H(признак)^d, и без
  d перебрать словарь нельзя, то есть закрыт наш T1;
- B не узнаёт ничего о признаках A: до него доходят только ослеплённые значения,
  и он видит лишь их количество;
- обе стороны получают одно и то же пересечение.

Модуль помечен экспериментальным: он НЕ подключён к матчингу. Матчинг переключается
на него только после решения основателя по замеру (шаг 3 задачи 1.5).

Оговорка для чтения цифр замера: экспоненцирование делает C-реализация
больших чисел Python, поэтому это честная оценка для схемы на больших числах,
но не для реализации на кривой через OpenSSL, где умножение точки дешевле.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from math import gcd
from secrets import randbelow

from cryptography.hazmat.primitives.asymmetric import rsa

MODULUS_BYTES = 256  # 2048 бит
MAX_FEATURES_PER_EXCHANGE = 64  # потолок из замера: дальше цена растёт линейно


def feature_hash(feature: str) -> int:
    """Признак в число по модулю: одно и то же у обеих сторон."""
    return int.from_bytes(hashlib.sha256(feature.encode("utf-8")).digest(), "big")


def blind_set(features: list[str], n: int, e: int) -> tuple[list[int], list[int]]:
    """Ослепить набор признаков. Возвращает значения и множители для снятия."""
    if len(features) > MAX_FEATURES_PER_EXCHANGE:
        raise ValueError("too_many_features")
    pairs = [blind(f, n, e) for f in features]
    return [value for value, _ in pairs], [factor for _, factor in pairs]


def unblind_set(signed: list[int], factors: list[int], n: int) -> list[int]:
    """Снять ослепление с подписанных значений."""
    if len(signed) != len(factors):
        raise ValueError("signed values and factors must have the same length")
    return [unblind(value, factor, n) for value, factor in zip(signed, factors)]


def matched_features(
    features: list[str], unblinded: list[int], holder_tokens: set[int]
) -> list[str]:
    """Что из своих признаков нашлось у держателя: подписи сравниваются как значения."""
    if len(features) != len(unblinded):
        raise ValueError("features and unblinded values must have the same length")
    return sorted(
        feature for feature, token in zip(features, unblinded) if token in holder_tokens
    )


@dataclass(frozen=True)
class Keys:
    """Ключ подписывающей стороны: N и e публичны, d остаётся у B."""

    n: int
    e: int
    d: int

    def public(self) -> tuple[int, int]:
        return self.n, self.e


def generate_keys() -> Keys:
    key = rsa.generate_private_key(public_exponent=65537, key_size=MODULUS_BYTES * 8)
    numbers = key.private_numbers()
    return Keys(n=numbers.public_numbers.n, e=numbers.public_numbers.e, d=numbers.d)


def _blind_factor(n: int) -> int:
    while True:
        r = randbelow(n - 3) + 2
        if gcd(r, n) == 1:
            return r


def blind(feature: str, n: int, e: int) -> tuple[int, int]:
    """Ослепить признак: (значение, множитель) для последующего снятия ослепления."""
    r = _blind_factor(n)
    blinded = feature_hash(feature) * pow(r, e, n) % n
    return blinded, r


def sign(blinded: int, d: int, n: int) -> int:
    """Подписать ослеплённое значение ключом стороны B."""
    return pow(blinded, d, n)


def unblind(signed: int, r: int, n: int) -> int:
    return signed * pow(r, -1, n) % n


def own_signed_tokens(features: list[str], keys: Keys) -> list[int]:
    """Свои признаки в подписанном виде: то, с чем сравнивает сторона A."""
    return [pow(feature_hash(f), keys.d, keys.n) for f in features]


def set_intersection(n: int, e: int, d: int, learner: list[str], holder: list[str]) -> list[str]:
    """Полный проход схемы, возвращает признаки learner, найденные у holder.

    Здесь обе стороны в одном процессе: это нужно для проверки корректности.
    В замере те же шаги считаются отдельно, чтобы честно измерить каждую сторону.
    """
    blinded = {f: blind(f, n, e) for f in learner}
    signed_back = {f: sign(b, d, n) for f, (b, _) in blinded.items()}
    unblinded = {f: unblind(s, blinded[f][1], n) for f, s in signed_back.items()}
    holder_tokens = set(own_signed_tokens(holder, Keys(n=n, e=e, d=d)))
    return sorted(f for f, token in unblinded.items() if token in holder_tokens)
