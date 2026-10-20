"""Signing of an agent card with JWS (RFC 7515, alg ES256).

A signature answers exactly one question: the card was not swapped out and it
came from whoever the key claims to be. It does NOT confirm that a live unique
human stands behind the agent. This boundary is recorded in LIMITATIONS and goes
into every evidence envelope.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from arli_engine.core.canonical import canonicalize

LIMITATIONS = [
    "A valid signature proves the card came from the holder of the key.",
    "A valid signature does NOT prove a unique human principal behind the agent.",
    "Revocation is out of scope here: a rotated key invalidates the card, not the identity.",
]

SIGNATURE_EXCLUDED_FIELD = "signatures"


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


@dataclass(frozen=True)
class KeyPair:
    private_pem: bytes
    public_pem: bytes

    @staticmethod
    def generate() -> "KeyPair":
        key = ec.generate_private_key(ec.SECP256R1())
        return KeyPair(
            private_pem=key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ),
            public_pem=key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ),
        )

    def load_private(self):
        return serialization.load_pem_private_key(self.private_pem, password=None)

    def load_public(self):
        return serialization.load_pem_public_key(self.public_pem)


def signing_payload(card: dict) -> bytes:
    """The canonical form of the card without the signatures field."""
    body = {k: v for k, v in card.items() if k != SIGNATURE_EXCLUDED_FIELD}
    return canonicalize(body)


def public_jwk(public_pem: bytes, kid: str = "") -> dict:
    """The public key as a JWK, so the card can be verified without an out-of-band channel."""
    key = serialization.load_pem_public_key(public_pem)
    if not isinstance(key, ec.EllipticCurvePublicKey):
        raise ValueError("public key must be an elliptic curve key for ES256")
    numbers = key.public_numbers()
    jwk = {
        "kty": "EC",
        "crv": "P-256",
        "x": b64u(numbers.x.to_bytes(32, "big")),
        "y": b64u(numbers.y.to_bytes(32, "big")),
        "alg": "ES256",
        "use": "sig",
    }
    if kid:
        jwk["kid"] = kid
    return jwk


def jwk_to_public_key(jwk: dict) -> ec.EllipticCurvePublicKey:
    if jwk.get("kty") != "EC" or jwk.get("crv") != "P-256":
        raise ValueError(f"unsupported jwk: kty={jwk.get('kty')!r} crv={jwk.get('crv')!r}")
    x = int.from_bytes(b64u_decode(jwk["x"]), "big")
    y = int.from_bytes(b64u_decode(jwk["y"]), "big")
    return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()



def sign_bytes(private_pem: bytes, kid: str, data: bytes) -> dict:
    """Sign raw bytes with the agent key, in the JWS form: ES256, raw R||S, base64url.

    Used by the identity method, where the point is exactly this: the bytes are chosen by whoever
    asks, so a signature over them cannot be replayed from anywhere else.
    """
    from cryptography.hazmat.primitives.asymmetric import utils as _utils

    key = serialization.load_pem_private_key(private_pem, password=None)
    der = key.sign(data, ec.ECDSA(hashes.SHA256()))
    r, s_value = _utils.decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s_value.to_bytes(32, "big")
    return {"alg": "ES256", "kid": kid, "value": b64u(raw)}

def sign_card(card: dict, private_pem: bytes, kid: str) -> dict:
    """Return a copy of the card with a JWS signature added.

    The public key is placed in the protected header (the jwk parameter, RFC
    7515 section 4.1.3), so another party can verify the card without receiving
    the key separately.
    """
    key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("private key must be an elliptic curve key for ES256")
    public_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    header = {"alg": "ES256", "kid": kid, "typ": "JOSE", "jwk": public_jwk(public_pem, kid)}
    protected = b64u(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload = b64u(signing_payload(card))
    signing_input = f"{protected}.{payload}".encode("ascii")

    der = key.sign(signing_input, ec.ECDSA(hashes.SHA256()))

    # JWS ES256 defines the signature value as the raw R||S pair, 64 bytes, not the DER form.
    # Ours was DER until a second implementation pointed this out: a third party verifying the
    # card by the JWS rules would have rejected it while our own verifier accepted it.
    r, s_value = utils.decode_dss_signature(der)
    raw = r.to_bytes(32, "big") + s_value.to_bytes(32, "big")

    signed = dict(card)
    signed[SIGNATURE_EXCLUDED_FIELD] = [
        {"protected": protected, "signature": b64u(raw)}
    ]
    return signed


def _verify_with_key(card: dict, key: ec.EllipticCurvePublicKey, protected: str, signature: str) -> tuple[bool, list[str]]:
    payload = b64u(signing_payload(card))
    signing_input = f"{protected}.{payload}".encode("ascii")
    raw = b64u_decode(signature)
    if len(raw) == 64:
        # The JWS form: R||S, 64 bytes, converted to DER only because the library wants DER.
        der = utils.encode_dss_signature(
            int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")
        )
    elif raw[:1] == b"\x30":
        return False, [
            "signature is DER: ES256 defines the value as the raw R||S pair, so this card "
            "would be rejected by a verifier that follows the JWS rules"
        ]
    else:
        return False, [f"signature is {len(raw)} bytes: ES256 needs 64"]
    try:
        key.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False, ["signature does not match the card content"]
    except Exception as exc:  # noqa: BLE001
        return False, [f"signature could not be verified: {exc}"]
    return True, []



def public_pem_from_card(card: dict) -> bytes | None:
    """The public key of the card's own signature header, as PEM.

    Used wherever a signed card has to stand for a key: the card binds a key to a name, so a
    signature that verifies against this key belongs to whoever the card names.
    """
    import base64

    from cryptography.hazmat.primitives import serialization

    signatures = card.get("signatures") or []
    if not signatures:
        return None
    protected = signatures[0].get("protected") or ""
    try:
        header = json.loads(b64u_decode(protected))
    except Exception:  # noqa: BLE001
        return None
    jwk = header.get("jwk") or {}
    if not jwk.get("x") or not jwk.get("y"):
        return None
    try:
        key = jwk_to_public_key(jwk)
    except Exception:  # noqa: BLE001
        return None
    return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

def verify_card_self(card: dict) -> tuple[bool, list[str]]:
    """Verify the card with the key from its own header.

    This is what any other party can do: the key arrives together with the card.
    Verification against a key known in advance stays in the separate function
    verify_card.
    """
    signatures = card.get(SIGNATURE_EXCLUDED_FIELD) or []
    if not signatures:
        return False, ["card carries no signature"]
    entry = signatures[0]
    protected, signature = entry.get("protected", ""), entry.get("signature", "")
    if not protected or not signature:
        return False, ["signature entry is incomplete"]
    try:
        header = json.loads(b64u_decode(protected))
    except Exception as exc:  # noqa: BLE001
        return False, [f"protected header is not base64url JSON: {exc}"]
    if header.get("alg") != "ES256":
        return False, [f"unsupported alg: {header.get('alg')!r}"]
    jwk = header.get("jwk")
    if not isinstance(jwk, dict):
        return False, ["card header carries no jwk: verification needs the key out of band"]
    try:
        key = jwk_to_public_key(jwk)
    except Exception as exc:  # noqa: BLE001
        return False, [f"jwk could not be read: {exc}"]
    return _verify_with_key(card, key, protected, signature)


def verify_card(card: dict, public_pem: bytes) -> tuple[bool, list[str]]:
    """Verify the signature. Returns (result, reasons for refusal)."""
    signatures = card.get(SIGNATURE_EXCLUDED_FIELD) or []
    if not signatures:
        return False, ["card carries no signature"]
    if len(signatures) != 1:
        return False, ["exactly one signature is supported in this prototype"]

    entry = signatures[0]
    protected = entry.get("protected", "")
    signature = entry.get("signature", "")
    if not protected or not signature:
        return False, ["signature entry is incomplete"]

    try:
        header = json.loads(b64u_decode(protected))
    except Exception as exc:  # noqa: BLE001
        return False, [f"protected header is not base64url JSON: {exc}"]

    if header.get("alg") != "ES256":
        return False, [f"unsupported alg: {header.get('alg')!r}"]

    key = serialization.load_pem_public_key(public_pem)
    if not isinstance(key, ec.EllipticCurvePublicKey):
        return False, ["public key is not an elliptic curve key for ES256"]
    # One verification path for cards, whether the key came from the header or from outside:
    # two paths is how the DER form survived in one of them for so long.
    return _verify_with_key(card, key, protected, signature)
