"""Согласование ключа пары и привязка отпечатков.

Ключ пары больше не передаётся по проводу. Он выводится согласованием на кривой
(те же ключи P-256, которыми подписывается карточка) и HKDF, где во вход идут
идентификаторы обоих агентов и идентификатор сессии. Отсюда два свойства:

- третья сторона ключа не знает и вывести его не может (T2 из модели угроз);
- отпечаток из другой сессии не совпадёт, потому что сессия входит и в ключ,
  и в сам отпечаток (T12).

Чего это НЕ решает и что записано в ограничениях: противная сторона по-прежнему
знает ключ, потому что обязана считать свои отпечатки, и по-прежнему может
перебирать словарь признаков (T1). Этот вопрос закрывается только закрытым
пересечением, и решение по нему вынесено отдельно (задача 1.5 плана).

Про привязку к стороне: она обязана быть общей для пары, иначе стороны посчитают
разные отпечатки одного и того же признака и сопоставление не сработает вовсе.
Поэтому в отпечаток входит каноническая метка пары, а не сторона отправителя.
Отражение (когда противная сторона присылает ваш же список) при этом не даёт ей
ничего нового: она получает обратно ваши же отпечатки.
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
    """Открытый ключ из закрытого. Нужен там, где сторона известна только закрытым ключом.

    Проверяющему нужен открытый ключ выдающего. В стенде и в движке он есть у стороны, но в
    некоторых путях (измерение, разовая проверка) на руках только закрытый. Выводить его
    честнее, чем складывать второй ключ, который потом разойдётся с первым.
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
    """Вывести ключ пары. По проводу он не ходит, обе стороны получают один и тот же."""
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
    """Отпечаток признака, привязанный к сессии и к паре.

    Метка пары обязана быть одинаковой у обеих сторон: иначе один и тот же
    признак даст разные отпечатки и сопоставление не сработает.
    """
    if not session_id or not pair:
        raise ValueError("a commitment must be bound to a session and to the pair")
    message = COMMIT_SEPARATOR.join((session_id, pair, feature)).encode("utf-8")
    return hmac.new(bytes.fromhex(pair_key_hex), message, hashlib.sha256).hexdigest()


def pair_label(agent_a: str, agent_b: str) -> str:
    """Каноническая метка пары: порядок не зависит от того, кто считает."""
    if not agent_a or not agent_b or agent_a == agent_b:
        raise ValueError("a pair label needs two different agents")
    return "|".join(sorted((agent_a, agent_b)))


# --- подпись конверта доказательства ---

def _envelope_signing_input(envelope: dict) -> bytes:
    """Тело конверта без самой подписи: идентификатор входит, поэтому он тоже защищён."""
    body = {k: v for k, v in envelope.items() if k != "signature"}
    return canonical_json(body).encode("utf-8")


def sign_envelope(envelope: dict, private_pem: bytes, kid: str) -> dict:
    """Подписать конверт ключом агента. Возвращает новый конверт с полем подписи."""
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
    """Проверить подпись конверта. Без неё доказательство неотличимо от подделки."""
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
