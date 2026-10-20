"""The agent card in the A2A 1.0 form and our extension to it.

The base card fields are taken as the A2A specification describes them:
name, description, version, provider, capabilities, skills, interfaces,
securitySchemes, signatures. Everything A2A lacks lives in a separate
extension arli.discovery.v1, so the card stays compatible.
"""

from __future__ import annotations

from arli_engine.core.capability import Operation, capability_manifest
from arli_engine.core.intent import (
    INTENT_REJECTIONS,
    INTENT_STATUSES,
    INTENT_TYPES,
    PROVENANCE,
)
from arli_engine.core.match import MATCH_REJECTIONS
from arli_engine.core.negotiate import ACTIONS, MAX_TURNS, OPENING_REJECTIONS, REJECTIONS
from arli_engine.core.psi import MAX_FEATURES_PER_EXCHANGE
from arli_engine.core.claims import CLAIM_LEVELS, CLAIM_REJECTIONS, CLAIM_STATES
from arli_engine.core.registry import KEY_REVOCATION_REASONS, REGISTRY_REJECTIONS
from arli_engine.core.surface import CALL_REJECTIONS, METHOD_NAMES, tool_name
from arli_engine.core.retention import RETENTION_DEFAULT_DAYS, RETENTION_REJECTIONS
from arli_engine.core.signing import sign_card

EXTENSION_URI = "https://arliwork.com/a2a/extensions/arli.discovery.v1"

# Our addition to the card: what is in neither A2A nor Index Network.
DEFAULT_LIMITATIONS = [
    "This prototype runs one matching algorithm; it is not a product.",
    "The card is self-declared until the evidence envelope is checked.",
]


def build_card(
    *,
    name: str,
    description: str,
    base_url: str,
    skills: list[dict],
    operations: list[Operation],
    public_features: list[str],
    kid: str | None = None,
    private_pem: bytes | None = None,
    version: str = "0.1.0",
    region: str = "eu-central",
) -> dict:
    """Build the card. If a key is passed, the card is signed right away."""
    card: dict = {
        "name": name,
        "description": description,
        "version": version,
        "provider": {
            "organization": "Shenzhen Capernaum Technology Co., Ltd.",
            "url": "https://arliwork.com",
        },
        "capabilities": {
            "streaming": False,
            "pushNotifications": False,
            "extendedAgentCard": False,
        },
        "skills": skills,
        "interfaces": [
            {
                "url": f"{base_url}/a2a/rpc",
                "protocolBinding": "JSONRPC",
                "protocolVersion": "1.0",
            }
        ],
        "securitySchemes": {
            "bearer": {"type": "http", "scheme": "bearer"},
        },
        "extensions": [
            {
                "uri": EXTENSION_URI,
                "description": (
                    "Verified discovery: honest capability manifest, local-only "
                    "matching over salted commitments, evidence on every claim."
                ),
                "required": False,
                "params": {
                    "capabilities_url": f"{base_url}/.well-known/arli-capabilities.json",
                    "matching": "local overlap of commitments bound to session and side",
                    "pair_key": "ECDH P-256 + HKDF over both agent ids and the session id, never transmitted",
                    "negotiation_grammar": "propose|counter|accept|decline, max 12 turns",
                    "consent": "agent agreement never opens a connection without principal approval",
                    "evidence": "envelopes are signed by the agent key; verification is required to read them",
                    "region": region,
                },
            }
        ],
        "signatures": [],
        # Public features are needed for matching, but the values themselves are not secret.
        "x-arli-public-feature-count": len(public_features),
        "x-arli-capabilities": capability_manifest(operations),
    }

    if private_pem is not None:
        card = sign_card(card, private_pem, kid or f"{name}-key-1")
    return card


def capability_document(operations: list[Operation]) -> dict:
    """A separate document so the card does not grow too large.

    Both closed lists of reasons are placed here too: another party reads them by
    machine rather than from documentation, and can check us for conformance.
    """
    return {
        "manifest": capability_manifest(operations),
        "extension_uri": EXTENSION_URI,
        "negotiation_rules": {
            "max_turns": MAX_TURNS,
            "actions": list(ACTIONS),
            "turn_rejections": list(REJECTIONS),
            "opening_rejections": list(OPENING_REJECTIONS),
            "note": (
                "Both lists are closed. A turn or a pair that is refused always "
                "carries one of these codes, and every refusal carries evidence."
            ),
        },
        "intersection": {
            "scheme": "closed_blind_rsa",
            "feature_cap": MAX_FEATURES_PER_EXCHANGE,
            "closed": "offline enumeration of the counterparty's features",
            "not_closed": (
                "online probing of a single guess, because any set intersection answers "
                "for every element submitted; bounded by one exchange per session"
            ),
            "callers_learn": "matches among their own features, and the holder learns the submission count",
            "match_rejections": list(MATCH_REJECTIONS),
        },
        "intent": {
            "types": list(INTENT_TYPES),
            "statuses": list(INTENT_STATUSES),
            "provenance": list(PROVENANCE),
            "comparable": "supply with demand, person with person; two supplies or two demands are not a match",
            "synthetic": "a network declares whether it admits synthetic intents; by default it does not",
            "intent_rejections": list(INTENT_REJECTIONS),
        },
        "bindings": {
            "a2a": {
                "transport": "http-jsonrpc",
                "rpc_path": "/a2a/rpc",
                "methods": list(METHOD_NAMES),
            },
            "mcp": {
                "transport": "stdio-jsonrpc",
                "naming": "a tool name is a method name with slashes replaced by underscores",
                "tools": [tool_name(method) for method in METHOD_NAMES],
            },
            "plugin": {
                "transport": "stdio-jsonrpc",
                "manifest": "arli-discovery",
                "tools": [tool_name(method) for method in METHOD_NAMES],
                "generated": "the manifest is built from the surface, never hand-written",
            },
            "rule": "every binding exposes the same surface: no method added, none hidden",
        },
        "registry": {
            "rotation": "a rotated key stops validating the card; the principal is untouched",
            "principal_revocation": "a separate record; it blocks new keys until the principal is confirmed again",
            "key_revocation_reasons": list(KEY_REVOCATION_REASONS),
            "registry_rejections": list(REGISTRY_REJECTIONS),
        },
        "call_rejections": list(CALL_REJECTIONS),
        "claims": {
            "levels": list(CLAIM_LEVELS),
            "states": list(CLAIM_STATES),
            "claim_rejections": list(CLAIM_REJECTIONS),
            "issuance": (
                "This implementation relays and verifies statements made by others and issues "
                "none of its own."
            ),
            "prohibitions": [
                "A level is not proof of who is behind an agent.",
                "A level is not transferable between networks.",
            ],
        },
        "retention": {
            "default_days": RETENTION_DEFAULT_DAYS,
            "raw_features": "never stored on the server",
            "archive": "marks the envelopes of an intent deleted and removes them from search",
            "purge": "physically removes records older than the window",
            "retention_rejections": list(RETENTION_REJECTIONS),
        },
        "limitations": DEFAULT_LIMITATIONS,
    }
