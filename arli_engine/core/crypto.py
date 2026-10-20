"""Pair key agreement and commitment binding.

The pair key is no longer sent over the wire. It is derived through curve
agreement (the same P-256 keys used to sign the card) and HKDF, whose input
carries the identifiers of both agents and the session identifier. Two properties
follow:

- a third party does not know the key and cannot derive it (T2 in the threat model);
- a commitment from another session will not match, because the session enters both
  the key and the commitment itself (T12).

What this does NOT solve, and what is recorded in the limitations: the opposite
party still knows the key, because it has to compute its own commitments, and it
can still enumerate a dictionary of features (T1). That is closed only by private
set intersection, and the decision on it is deferred (task 1.5 of the plan).

On binding to a party: it must be shared across the pair, otherwise the two sides
would compute different commitments for the same feature and matching would not
work at all. So the commitment includes the canonical pair label, not the sender's
side. Reflection (when the opposite party sends back your own list) gives it
nothing new: it gets your own commitments back.
"""

from __future__ import annotations

import hashlib
import hmac

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from arli_engine.core.canonical import canonical_json

HKDF_INFO = b"arli.discovery.v1/pair"
COMMIT_SEPARATOR = "\x1f"


def _load_private(pem: bytes) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise TypeError("pair key agreement needs an elliptic curve private key")
    return key


def _load_public(pem: bytes) -> ec.EllipticCurvePublicKey:
    key = serialization.load_pem_public_key(pem)
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise TypeError("pair key agreement needs an elliptic curve public key")
    return key


def public_pem_from_private(private_pem: bytes) -> bytes:
    """The public key from the private one. Needed where a party is known only by its private key.

    A verifier needs the issuer's public key. In the harness and in the engine the
    party has it, but on some paths (measurement, one-off verification) only the
    private key is at hand. Deriving it is more honest than storing a second key
    that would later drift from the first.
    """
    return _load_private(private_pem).public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def derive_pair_key(
    own_private_pem: bytes,
    peer_public_pem: bytes,
    *,
    session_id: str,
    agent_a: str,
    agent_b: str,
) -> str:
    """Derive the pair key. It does not travel over the wire; both sides get the same one."""
    if not session_id:
        raise ValueError("session_id is required: a key without a session can be replayed")
    if not agent_a or not agent_b:
        raise ValueError("both agent identifiers are required to bind the key")
    if agent_a == agent_b:
        raise ValueError("a key needs two different agents")

    shared = _load_private(own_private_pem).exchange(ec.ECDH(), _load_public(peer_public_pem))
    salt = ("|".join(sorted((agent_a, agent_b))) + "|" + session_id).encode("utf-8")
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=HKDF_INFO).derive(shared).hex()


def commitment(pair_key_hex: str, *, session_id: str, pair: str, feature: str) -> str:
    """A commitment for a feature, bound to the session and to the pair.

    The pair label must be the same on both sides: otherwise the same feature
    would yield different commitments and matching would not work.
    """
    if not session_id or not pair:
        raise ValueError("a commitment must be bound to a session and to the pair")
    message = COMMIT_SEPARATOR.join((session_id, pair, feature)).encode("utf-8")
    return hmac.new(bytes.fromhex(pair_key_hex), message, hashlib.sha256).hexdigest()


def pair_label(agent_a: str, agent_b: str) -> str:
    """The canonical pair label: the order does not depend on who computes it."""
    if not agent_a or not agent_b or agent_a == agent_b:
        raise ValueError("a pair label needs two different agents")
    return "|".join(sorted((agent_a, agent_b)))


# --- evidence envelope signature ---

def _envelope_signing_input(envelope: dict) -> bytes:
    """The envelope body without the signature itself: the identifier is included, so it is protected too."""
    body = {k: v for k, v in envelope.items() if k != "signature"}
    return canonical_json(body).encode("utf-8")


def sign_envelope(envelope: dict, private_pem: bytes, kid: str) -> dict:
    """Sign the envelope with the agent key. Returns a new envelope with a signature field."""
    key = _load_private(private_pem)
    der = key.sign(_envelope_signing_input(envelope), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    size = 32
    raw = r.to_bytes(size, "big") + s.to_bytes(size, "big")
    import base64

    signed = dict(envelope)
    signed["signature"] = {
        "alg": "ES256",
        "kid": kid,
        "value": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
    }
    return signed


def verify_envelope(envelope: dict, public_pem: bytes) -> tuple[bool, list[str]]:
    """Verify the envelope signature. Without it, evidence is indistinguishable from a forgery."""
    import base64

    errors: list[str] = []
    sig = envelope.get("signature")
    if not isinstance(sig, dict) or not sig.get("value"):
        return False, ["envelope has no signature"]
    try:
        raw = base64.urlsafe_b64decode(sig["value"] + "=" * (-len(sig["value"]) % 4))
    except Exception:
        return False, ["signature value is not base64url"]
    if len(raw) != 64:
        return False, ["signature must be 64 bytes for ES256"]
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    der = utils.encode_dss_signature(r, s)
    key = _load_public(public_pem)
    try:
        key.verify(der, _envelope_signing_input(envelope), ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        errors.append("signature does not match the envelope body")
        return False, errors
    return True, errors
