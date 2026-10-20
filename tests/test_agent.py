"""The command a participant actually runs: one agent, one key, one address.

Three things are load-bearing here and each has a way to go quietly wrong: the key must survive a
restart (otherwise the address keeps its name but changes its identity), the card must publish the
count and not the values (otherwise the whole point of local matching is gone), and the health
document must answer without a card fetch.
"""

from __future__ import annotations

import json
import stat
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import arli_engine
from arli_engine.adapters.a2a.server import health_document, start_agent  # noqa: E402
from arli_engine.agent import build_agent, load_or_create_key  # noqa: E402
from arli_engine.core.signing import verify_card_self  # noqa: E402

FEATURES = ["skill:brand-design", "city:shenzhen", "role:studio"]


def test_the_key_is_created_owner_only_and_then_reused(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "agent.pem"
    first, created = load_or_create_key(path)
    assert created is True
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"the private key must be owner-only, found {oct(mode)}"

    second, created_again = load_or_create_key(path)
    assert created_again is False
    assert second.private_pem == first.private_pem, "a restart must not change the agent's key"
    assert second.public_pem == first.public_pem


def test_the_card_carries_the_extension_version() -> None:
    """A visitor has to be able to tell which text an agent speaks without asking it."""
    agent = build_agent("card-check", FEATURES, "http://127.0.0.1:8811")
    entry = agent.card["extensions"][0]
    assert entry["uri"].endswith("arli.discovery.v1")
    assert entry["version"] == arli_engine.EXTENSION_VERSION


def test_the_card_publishes_the_count_and_never_the_values() -> None:
    agent = build_agent("count-check", FEATURES, "http://127.0.0.1:8811")
    raw = json.dumps(agent.card)
    assert agent.card["x-arli-public-feature-count"] == len(FEATURES)
    for value in FEATURES:
        assert value not in raw, f"{value} must not appear in the card"


def test_the_card_a_participant_serves_verifies(tmp_path: Path) -> None:
    key, _created = load_or_create_key(tmp_path / "k.pem")
    agent = build_agent("verify-check", FEATURES, "http://127.0.0.1:8811", private_pem=key.private_pem)
    verified, reasons = verify_card_self(agent.card)
    assert verified, reasons


def test_the_health_document_answers_without_a_card_fetch() -> None:
    agent = build_agent("health-check", FEATURES, "http://127.0.0.1:8811")
    document = health_document(agent)
    assert document["ok"] is True
    assert document["agent"] == "health-check"
    assert document["extension_version"] == arli_engine.EXTENSION_VERSION
    assert document["features_held_at_home"] == len(FEATURES)
    assert document["kid"] == "health-check-key-1"
    assert not any(value in json.dumps(document) for value in FEATURES)


def test_the_agent_answers_on_a_real_port(tmp_path: Path) -> None:
    """The three addresses a visitor touches, answered by a real server rather than a handler."""
    key, _created = load_or_create_key(tmp_path / "k.pem")
    agent = build_agent("live-check", FEATURES, "http://127.0.0.1:0", private_pem=key.private_pem)
    server, _thread = start_agent(agent, 0, host="127.0.0.1")
    try:
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with urllib.request.urlopen(f"{base}/api/health", timeout=10) as answer:
            health = json.load(answer)
        assert health["ok"] is True and health["agent"] == "live-check"
        with urllib.request.urlopen(f"{base}/.well-known/agent-card.json", timeout=10) as answer:
            card = json.load(answer)
        assert card["name"] == "live-check"
        assert verify_card_self(card)[0]
        with urllib.request.urlopen(f"{base}/.well-known/arli-capabilities.json", timeout=10) as answer:
            capabilities = json.load(answer)
        assert capabilities
    finally:
        server.shutdown()
        server.server_close()
