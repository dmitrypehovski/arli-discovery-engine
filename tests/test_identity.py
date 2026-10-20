"""Tests for the identity method: signing a nonce the asker chose.

Why these exist. This method is the one that can be abused: an agent that signs whatever it is
handed is a signing oracle, useful for forging statements in that agent's name. So the tests are
less about the happy path and more about the two bounds in the text: a nonce has to be long enough
to be worth signing, a purpose has to be named, and the same address cannot ask twice in a minute.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arli_engine.client import AgentClient, AgentRefused  # noqa: E402
from arli_engine.core.signing import public_pem_from_card  # noqa: E402
from arli_engine.serve import build_pair, port_of  # noqa: E402

NONCE = "nonce-for-the-identity-test-0001"


@pytest.fixture()
def agent():
    states, servers = build_pair(0, 0, names=("signer", "peer"))
    try:
        yield f"http://127.0.0.1:{port_of(servers[0])}"
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def _verify(public_pem: bytes, nonce: str, signature_b64: str) -> bool:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    raw = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
    assert len(raw) == 64, "ES256 signatures are 64 bytes, raw R||S"
    der = utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    key = load_pem_public_key(public_pem)
    try:
        key.verify(der, nonce.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        return True
    except Exception:  # noqa: BLE001
        return False


def test_the_address_proves_it_holds_the_key(agent: str) -> None:
    """The whole point: the signature verifies with the key from the agent's own card."""
    with AgentClient(agent) as client:
        card = client.get_card()
        answer = client.call("arli/identity/sign", {"nonce": NONCE, "purpose": "connect"})
    assert answer["alg"] == "ES256"
    assert answer["kid"], "the answer must name the key that signed"
    assert answer["signed_bytes"] == len(NONCE.encode("utf-8")), "the bytes are signed as sent"
    assert _verify(public_pem_from_card(card), NONCE, answer["signature"]), (
        "the signature must verify with the key the card names"
    )


def test_a_short_nonce_is_refused(agent: str) -> None:
    with AgentClient(agent) as client:
        with pytest.raises(AgentRefused) as refusal:
            client.call("arli/identity/sign", {"nonce": "short", "purpose": "connect"})
    assert refusal.value.code == "nonce_too_short"


def test_a_purpose_is_required(agent: str) -> None:
    with AgentClient(agent) as client:
        with pytest.raises(AgentRefused) as refusal:
            client.call("arli/identity/sign", {"nonce": NONCE, "purpose": "   "})
    assert refusal.value.code == "purpose_required"


def test_the_same_address_cannot_ask_twice_in_a_minute(agent: str) -> None:
    """Bound one: an agent that signs anything on demand is a forging tool."""
    with AgentClient(agent) as client:
        client.call("arli/identity/sign", {"nonce": NONCE, "purpose": "connect"})
        with pytest.raises(AgentRefused) as refusal:
            client.call("arli/identity/sign", {"nonce": NONCE + "2", "purpose": "connect"})
    assert refusal.value.code == "rate_limited"


def test_a_different_purpose_has_its_own_window(agent: str) -> None:
    """The window is per address and purpose, so an honest second use is not blocked."""
    with AgentClient(agent) as client:
        first = client.call("arli/identity/sign", {"nonce": NONCE, "purpose": "connect"})
        second = client.call("arli/identity/sign", {"nonce": NONCE, "purpose": "audit"})
    assert first["signature"] and second["signature"]


def test_the_signature_is_over_the_bytes_sent_not_a_hash_of_a_string(agent: str) -> None:
    """A signature over a re-encoded nonce would be useless: the asker must be able to check it."""
    with AgentClient(agent) as client:
        card = client.get_card()
        answer = client.call("arli/identity/sign", {"nonce": NONCE, "purpose": "connect"})
    public_pem = public_pem_from_card(card)
    assert _verify(public_pem, NONCE, answer["signature"])
    assert not _verify(public_pem, NONCE + "x", answer["signature"]), "a different nonce must not verify"


def test_the_window_follows_the_caller_behind_a_proxy(agent: str) -> None:
    """Behind a proxy every visitor looks like one address, so a stranger could block yours.

    The forwarded header names the caller. Two different callers in the same minute both get an
    answer, and the bound still holds for each of them separately.
    """
    import json
    import urllib.error
    import urllib.request

    def ask(forwarded: str) -> dict:
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "arli/identity/sign",
                           "params": {"nonce": "a-nonce-long-enough-to-sign-77", "purpose": "connect"}}).encode()
        request = urllib.request.Request(f"{agent}/a2a/rpc", data=body,
                                         headers={"Content-Type": "application/json",
                                                   "X-Forwarded-For": forwarded})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            return json.loads(error.read())

    first = ask("203.0.113.7")
    second = ask("203.0.113.8")
    third = ask("203.0.113.7")
    assert "result" in first, first
    assert "result" in second, "a second caller in the same minute must not inherit the first window"
    assert "error" in third and "rate_limited" in third["error"]["message"]


def test_a_signature_over_exact_bytes_can_be_checked_by_a_stranger() -> None:
    """The counterpart of sign_bytes: the same rules, in one place, so two sides cannot disagree."""
    from arli_engine.core.signing import KeyPair, public_jwk, sign_bytes, verify_signed_bytes

    pair = KeyPair.generate()
    jwk = public_jwk(pair.public_pem, "kid-under-test")
    body = b'{"agreement":"what was agreed"}'
    signature = sign_bytes(pair.private_pem, "kid-under-test", body)
    signature["jwk"] = jwk  # a stranger gets the key with the signature, not from anywhere else

    ok, reasons = verify_signed_bytes(jwk, body, signature)
    assert ok is True, reasons
    assert reasons == []

    ok, reasons = verify_signed_bytes(jwk, b'{"agreement":"something else"}', signature)
    assert ok is False and "signature_does_not_hold" in reasons, (
        "a signature over other bytes must not verify: that is the whole point of signing these bytes"
    )

    broken = dict(signature, value=signature["value"][:-4] + "AAAA")
    ok, reasons = verify_signed_bytes(jwk, body, broken)
    assert ok is False, reasons
