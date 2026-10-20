"""One key per epoch: what it saves, and what it must not become.

The epoch exists because the holder was making a key and recomputing signatures over its own list for
every seeker. What it must never become is a token table that lives for ever: an epoch that does not expire
is a dictionary an attacker accumulates against, and a revoked agent must stop being served.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arli_engine.core.psi import new_epoch  # noqa: E402
from arli_engine.serve import build_pair, port_of  # noqa: E402
from arli_engine.client import AgentClient  # noqa: E402

FEATURES = ["offer:warehouse", "offer:freight", "offer:customs"]


def test_the_holders_own_tokens_are_computed_once_and_then_reused() -> None:
    epoch = new_epoch()
    first = epoch.tokens_for(FEATURES)
    after_first = len(epoch._tokens)
    second = epoch.tokens_for(FEATURES)
    assert first == second, "the same list gives the same tokens inside one epoch"
    assert len(epoch._tokens) == after_first, "the second call computed nothing new, it reused the table"
    assert after_first == len(FEATURES)


def test_an_epoch_expires() -> None:
    epoch = new_epoch(ttl_seconds=1)
    created = epoch.created_at
    assert not epoch.expired(created), "fresh"
    assert epoch.expired(created + 1), "a table that lives for ever is what this must not be"


def test_one_key_across_sessions_and_a_new_one_after_expiry() -> None:
    states, servers = build_pair(0, 0, names=("holder", "seeker"))
    holder = states[1]
    try:
        with AgentClient(f"http://127.0.0.1:{port_of(servers[1])}") as client:
            first = client.call("arli/psi/keys", {"session_id": "one"})
            second = client.call("arli/psi/keys", {"session_id": "two"})
            assert first["n"] == second["n"], "both seekers in an epoch share one key"
            assert first["epoch_id"] == second["epoch_id"]
            assert first["epoch_expires_in_seconds"] > 0, "the response says how long the key lives"

            holder.epoch.ttl_seconds = 0
            holder.epoch.created_at = time.time() - 1
            third = client.call("arli/psi/keys", {"session_id": "three"})
            assert third["epoch_id"] != first["epoch_id"], "an expired epoch is replaced, not extended"
            assert third["n"] != first["n"]
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_signing_the_seekers_values_still_costs_per_exchange() -> None:
    """The epoch removes key generation and token recomputation, never the signing of the seeker's list."""
    states, servers = build_pair(0, 0, names=("holder", "seeker"))
    holder = states[1]
    holder.features = FEATURES
    try:
        with AgentClient(f"http://127.0.0.1:{port_of(servers[1])}") as client:
            keys = client.call("arli/psi/keys", {"session_id": "s1"})
            commit = client.call("arli/psi/commit", {
                "session_id": "s1", "peer_name": "seeker", "n": keys["n"], "e": keys["e"]})
            signed = client.call("arli/psi/sign", {
                "session_id": "s1", "blinded": commit["blinded"][:len(FEATURES)]})
            assert len(signed["signed"]) == len(FEATURES), "the seeker's values were signed in this exchange"
            assert signed["epoch_id"] == keys["epoch_id"], "the signature belongs to the epoch's key"
            assert len(signed["holder_tokens"]) == len(FEATURES)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_a_session_bound_to_an_expired_epoch_is_refused_not_signed() -> None:
    """A seeker that fetched a key under an old epoch must not be signed under the new one."""
    states, servers = build_pair(0, 0, names=("holder", "seeker"))
    holder = states[1]
    try:
        with AgentClient(f"http://127.0.0.1:{port_of(servers[1])}") as client:
            keys = client.call("arli/psi/keys", {"session_id": "stale"})
            commit = client.call("arli/psi/commit", {
                "session_id": "stale", "peer_name": "seeker", "n": keys["n"], "e": keys["e"]})
            holder.epoch.ttl_seconds = 0
            holder.epoch.created_at = time.time() - 1
            from arli_engine.client.agent import AgentRefused
            try:
                client.call("arli/psi/sign", {
                    "session_id": "stale", "blinded": commit["blinded"][:2]})
                raise AssertionError("a session whose epoch expired must be refused")
            except AgentRefused as refused:
                assert "missing_private_key" in str(refused), str(refused)
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
