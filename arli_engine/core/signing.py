"""Подпись карточки агента по JWS (RFC 7515, alg ES256).

Подпись отвечает ровно на один вопрос: карточка не подменена и пришла от
того, за кого себя выдаёт ключ. Она НЕ подтверждает, что за агентом стоит
живой уникальный человек. Эта граница записана в LIMITATIONS и попадает
в каждый конверт доказательства.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

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
    """Канонический вид карточки без поля подписей."""
    body = {k: v for k, v in card.items() if k != SIGNATURE_EXCLUDED_FIELD}
    return canonicalize(body)


def public_jwk(public_pem: bytes, kid: str = "") -> dict:
    """Открытый ключ в виде JWK, чтобы карточку можно было проверить без внешнего канала."""
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


def sign_card(card: dict, private_pem: bytes, kid: str) -> dict:
    """Вернуть копию карточки с добавленной подписью JWS.

    Открытый ключ кладётся в защищённый заголовок (параметр jwk, RFC 7515 §4.1.3),
    поэтому чужая сторона может проверить карточку, не получая ключ отдельно.
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

    signed = dict(card)
    signed[SIGNATURE_EXCLUDED_FIELD] = [
        {"protected": protected, "signature": b64u(der)}
    ]
    return signed


def _verify_with_key(card: dict, key: ec.EllipticCurvePublicKey, protected: str, signature: str) -> tuple[bool, list[str]]:
    payload = b64u(signing_payload(card))
    signing_input = f"{protected}.{payload}".encode("ascii")
    try:
        key.verify(b64u_decode(signature), signing_input, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False, ["signature does not match the card content"]
    except Exception as exc:  # noqa: BLE001
        return False, [f"signature could not be verified: {exc}"]
    return True, []


def verify_card_self(card: dict) -> tuple[bool, list[str]]:
    """Проверить карточку по ключу из её же заголовка.

    Это то, что может сделать любая чужая сторона: ключ приходит вместе с карточкой.
    Проверка по заранее известному ключу остаётся отдельной функцией verify_card.
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
    """Проверить подпись. Возвращает (результат, причины отказа)."""
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

    payload = b64u(signing_payload(card))
    signing_input = f"{protected}.{payload}".encode("ascii")

    key = serialization.load_pem_public_key(public_pem)
    if not isinstance(key, ec.EllipticCurvePublicKey):
        return False, ["public key is not an elliptic curve key for ES256"]
    try:
        key.verify(b64u_decode(signature), signing_input, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return False, ["signature does not match the card content"]
    except Exception as exc:  # noqa: BLE001
        return False, [f"signature could not be verified: {exc}"]
    return True, []
