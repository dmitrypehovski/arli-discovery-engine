"""JCS-подобная каноникализация для подписи карточек агентов.

RFC 8785 требует сортировки ключей по код-поинтам UTF-16, минимальных
экранирований и отсутствия незначащих пробелов. Здесь реализуется тот
подмножества, которое использует карточка агента (строки, целые, булевы,
null, массивы, объекты). Числа с плавающей точкой в карточку не кладём:
их представление в JCS отличается от json.dumps, и это записано как
ограничение в LIMITATIONS.
"""

from __future__ import annotations

import json
from typing import Any

LIMITATIONS = [
    "Floats are rejected: RFC 8785 number serialization differs from json.dumps.",
    "Only the JSON subset used by Agent Cards is supported (str, int, bool, None, list, dict).",
]


class NotCanonicalizable(ValueError):
    """Значение нельзя каноникализировать по нашим правилам."""


def _check(value: Any, path: str = "$") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        raise NotCanonicalizable(f"float at {path}: {LIMITATIONS[0]}")
    if isinstance(value, list):
        for i, item in enumerate(value):
            _check(item, f"{path}[{i}]")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str):
                raise NotCanonicalizable(f"non-string key at {path}")
            _check(v, f"{path}.{k}")
        return
    raise NotCanonicalizable(f"unsupported type at {path}: {type(value).__name__}")


def canonicalize(value: Any) -> bytes:
    """Вернуть канонический UTF-8 байтовый вид значения."""
    _check(value)
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def canonical_json(value: Any) -> str:
    """То же, но строкой (для показа и для хешей содержимого)."""
    return canonicalize(value).decode("utf-8")
