"""JCS-like canonicalization for signing agent cards.

RFC 8785 requires sorting keys by UTF-16 code points, minimal escaping and no
insignificant whitespace. This implements the subset that an agent card uses
(strings, integers, booleans, null, arrays, objects). We do not put floating
point numbers into the card: their representation in JCS differs from
json.dumps, and that is recorded as a limitation in LIMITATIONS.
"""

from __future__ import annotations

import json
from typing import Any

LIMITATIONS = [
    "Floats are rejected: RFC 8785 number serialization differs from json.dumps.",
    "Only the JSON subset used by Agent Cards is supported (str, int, bool, None, list, dict).",
]


class NotCanonicalizable(ValueError):
    """The value cannot be canonicalized by our rules."""


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
    """Return the canonical UTF-8 byte form of the value."""
    _check(value)
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def canonical_json(value: Any) -> str:
    """The same, but as a string (for display and for content hashes)."""
    return canonicalize(value).decode("utf-8")
