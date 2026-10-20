"""The published vocabulary: what counts as the same thing on both sides.

Why this exists. Without it, a buyer writing ``need:warehouse`` and a supplier writing
``offer:warehouse`` share no string at all, so two sides of the same trade match only on incidental
attributes like the city. The intersection compares tokens, so the fix is one rule both sides apply
independently: each feature is reduced to a canonical token before it is hashed.

The rule, in full, and it is the whole rule:

- Lower case, trim, and collapse runs of spaces, hyphens and underscores into one hyphen.
- A feature split at the first colon is (namespace, value). No colon means the value alone.
- Families of namespaces:
  - **goods**: ``need``, ``want``, ``offer``, ``supply``, ``skill``, ``service``, ``commodity``,
    ``product``. A goods feature reduces to its value, so a need and an offer of the same commodity
    are the same token.
  - **attributes**: everything else, including ``city``, ``industry``, ``role``, ``capacity``,
    ``compliance``, ``stage``, ``region``, ``language``. An attribute keeps its namespace, so
    ``city:berlin`` never collides with ``industry:berlin``.
- An unknown namespace is treated as an attribute. Being wrong in that direction only fails to match,
  while being wrong the other way would create a match nobody declared.

What this is not. It is not an understanding of meaning: two words that mean the same thing but are
spelled differently do not meet unless the vocabulary says so. The vocabulary is short, published and
identical on both sides, which is what makes it checkable.
"""

from __future__ import annotations

import re

GOODS_NAMESPACES = frozenset({
    "need", "want", "offer", "supply", "skill", "service", "commodity", "product",
})

_WHITESPACE_RUNS = re.compile(r"[\s_-]+")


def _clean(text: str) -> str:
    return _WHITESPACE_RUNS.sub("-", text.strip().lower()).strip("-")


def canonical_token(feature: str) -> str:
    """The one string both sides hash for a feature. Documented above, and the same in both
    implementations: if the two ever disagree here, a side starts matching against a different rule."""
    text = str(feature or "")
    if ":" in text:
        namespace, value = text.split(":", 1)
        namespace = _clean(namespace)
        value = _clean(value)
        if namespace in GOODS_NAMESPACES:
            return value
        return f"{namespace}:{value}"
    return _clean(text)


def family_of(feature: str) -> str:
    """Which family a feature belongs to, for reporting and for tests."""
    if ":" not in str(feature or ""):
        return "goods"
    namespace = _clean(str(feature).split(":", 1)[0])
    return "goods" if namespace in GOODS_NAMESPACES else namespace