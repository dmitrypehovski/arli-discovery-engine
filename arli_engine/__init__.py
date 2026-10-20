"""ARLI Discovery Engine.

Ядро обнаружения агентов без транспорта. Спецификация расширения и набор
проверок соответствия.

Границы слоёв описаны в разделе 10 плана: из ``arli_engine.core`` не должно
быть ни одного импорта сети, потоков и HTTP.

Версии в одном месте. Версия пакета это версия кода, версия расширения это версия
протокольного текста. Они не обязаны совпадать и не совпадают: текст уже на 1.0.0,
а код идёт к этому отдельными шагами. Смешивать их в одну цифру значит врать в одной
из двух сторон.
"""

__version__ = "0.1.0"
EXTENSION_VERSION = "1.1.0"

from arli_engine.adapters.a2a.cards import EXTENSION_URI, build_card, capability_document
from arli_engine.core.canonical import canonical_json, canonicalize
from arli_engine.core.capability import EFFECT_KEYS, Operation, capability_manifest
from arli_engine.core.evidence import Completeness, Evidence
from arli_engine.core.match import FeatureSet, PairSession, local_match
from arli_engine.core.negotiate import MAX_TURNS, Negotiation, NegotiationError, Opportunity
from arli_engine.core.signing import KeyPair, sign_card, verify_card

__all__ = [
    "EXTENSION_URI",
    "EXTENSION_VERSION",
    "__version__",
    "EFFECT_KEYS",
    "MAX_TURNS",
    "Completeness",
    "Evidence",
    "FeatureSet",
    "KeyPair",
    "Negotiation",
    "NegotiationError",
    "Operation",
    "Opportunity",
    "PairSession",
    "build_card",
    "canonical_json",
    "canonicalize",
    "capability_document",
    "capability_manifest",
    "local_match",
    "sign_card",
    "verify_card",
]
