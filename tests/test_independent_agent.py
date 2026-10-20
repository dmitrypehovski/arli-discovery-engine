"""The claim "any harness can join" is tested against a harness that is not ours.

The agent in `examples/independent_agent.py` is written from the text and imports nothing from this
engine. These tests keep that true, and keep the two things the network promises: the address can
prove it holds its key, and an exchange with it reaches a recorded intersection without either side
learning the other's list.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.network import run_remote_exchange, verify_endpoint  # noqa: E402
from arli_engine.client import AgentClient  # noqa: E402
from arli_engine.core.signing import public_pem_from_card  # noqa: E402
from arli_engine.core.signing import verify_card_self  # noqa: E402

FEATURES = ["skill:design", "city:sf", "offer:infra", "role:investor", "need:brand-design"]


def _load_example():
    spec = importlib.util.spec_from_file_location("independent_agent", ROOT / "examples" / "independent_agent.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def foreign():
    """A real server, on a real port, running the independent agent's own code."""
    module = _load_example()
    agent = module.Agent("indie-test", list(FEATURES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), module.make_handler(agent))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield module, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_the_independent_agent_imports_nothing_from_the_engine() -> None:
    """If it ever leaned on our code, the proof would be worthless."""
    source = (ROOT / "examples" / "independent_agent.py").read_text(encoding="utf-8")
    assert "arli_engine" not in source, "the independent agent must not import the engine"
    assert "from arli" not in source and "import arli" not in source


def test_a_foreign_card_verifies_by_our_own_checker(foreign) -> None:
    _module, endpoint = foreign
    verified = verify_endpoint(endpoint)
    assert verified["name"] == "indie-test"
    assert public_pem_from_card(verified["card"]), "the card must carry the key it names"


def test_a_foreign_address_proves_it_holds_its_key(foreign) -> None:
    module, endpoint = foreign
    nonce = "a-nonce-this-engine-chose-0001"
    with AgentClient(endpoint) as client:
        answer = client.call("arli/identity/sign", {"nonce": nonce, "purpose": "connect"})
    assert answer["alg"] == "ES256" and answer["signed_bytes"] == len(nonce)
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    import base64

    card = verify_endpoint(endpoint)["card"]
    raw = base64.urlsafe_b64decode(answer["signature"] + "=" * (-len(answer["signature"]) % 4))
    der = utils.encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    load_pem_public_key(public_pem_from_card(card)).verify(der, nonce.encode(), ec.ECDSA(hashes.SHA256()))


def test_the_foreign_agent_refuses_a_second_signing_in_the_same_minute(foreign) -> None:
    module, endpoint = foreign
    with AgentClient(endpoint) as client:
        client.call("arli/identity/sign", {"nonce": "a-nonce-this-engine-chose-0002", "purpose": "connect"})
        with pytest.raises(Exception) as refusal:
            client.call("arli/identity/sign", {"nonce": "a-nonce-this-engine-chose-0003", "purpose": "connect"})
    assert "rate_limited" in str(refusal.value)


def test_an_exchange_with_a_foreign_harness_reaches_the_intersection(foreign, monkeypatch) -> None:
    """The whole point: our side is the engine, their side is not, and it still matches."""
    monkeypatch.setenv("ARLI_MATCH_ALLOW_PRIVATE", "1")
    module, endpoint = foreign
    outcome = run_remote_exchange(
        ["need:infra", "skill:design", "city:sf", "role:founder"],
        endpoint,
        floor=3000,
        remote_id="indie-test",
        remote_kind="supply",
    )
    assert outcome["opened"], outcome.get("rejection")
    assert outcome.get("matched_features") == ["city:sf", "skill:design"], outcome
    assert outcome["overlap_basis_points"] == 5000
    assert outcome["passed_floor"] is True
    assert outcome["agreed"], "the negotiation must reach a recorded agreement"
    assert outcome["wires_clean"] and not outcome["leaked_features"], (
        "no feature value may travel on the wire"
    )


def test_a_foreign_agent_with_nothing_in_common_is_below_the_floor(foreign, monkeypatch) -> None:
    monkeypatch.setenv("ARLI_MATCH_ALLOW_PRIVATE", "1")
    module, endpoint = foreign
    outcome = run_remote_exchange(
        ["skill:forging", "city:harbin"],
        endpoint,
        floor=3000,
        remote_id="indie-test",
        remote_kind="supply",
    )
    assert outcome["opened"] and outcome["agreed"] and outcome["passed_floor"] is False
    assert not outcome["matched_features"]
