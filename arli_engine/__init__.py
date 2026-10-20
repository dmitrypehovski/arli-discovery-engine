"""ARLI Discovery Engine.

Transport-free agent discovery core. The extension specification and the
conformance check suite.

The layer boundaries are described in section 10 of the plan: there must not be
a single import of network, threads or HTTP anywhere under ``arli_engine.core``.

Versions live in one place. The package version is the version of the code, the
extension version is the version of the protocol text. They do not have to match and
do not match: the text is already at 1.0.0 while the code gets there in separate
steps. Merging them into a single number would mean lying about one of the two sides.
"""

__version__ = "0.1.0"
EXTENSION_VERSION = "1.2.0"

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
