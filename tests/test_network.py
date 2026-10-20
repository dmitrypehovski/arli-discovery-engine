"""Tests for connecting somebody else's agent, and for the reach guards.

Why these exist. An endpoint is supplied by a stranger, so the checks that stand between our server
and that stranger's address are the most important code here. The one known gap, negotiation across
two separate processes, is recorded as a test rather than as prose: when the witness step lands,
this test fails and gets rewritten.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.NamedTemporaryFile(prefix="arli-network-test-", suffix=".sqlite3", delete=False)
os.environ["ARLI_MATCH_DB"] = _TMP.name
os.environ["ARLI_MATCH_ALLOW_PRIVATE"] = "1"

from app import network, service  # noqa: E402
from arli_engine.serve import build_pair, port_of  # noqa: E402

service.init()


def test_reach_guards_refuse_what_they_should() -> None:
    for endpoint, code in (
        ("ftp://example.com", "unsupported_scheme"),
        ("http://", "bad_endpoint"),
        ("", "missing_endpoint"),
    ):
        try:
            network.normalize_endpoint(endpoint, allow_private=False)
        except network.ConnectRefused as error:
            assert error.code == code, (endpoint, error.code)
        else:
            raise AssertionError(f"accepted {endpoint}")

    try:
        network.normalize_endpoint("http://127.0.0.1:9000", allow_private=False)
    except network.ConnectRefused as error:
        assert error.code == "address_not_reachable", error.code
    else:
        raise AssertionError("a loopback address must be refused by default")

    # With the local-test switch it is allowed, and the path is normalised.
    assert network.normalize_endpoint("http://127.0.0.1:9000/", allow_private=True) == "http://127.0.0.1:9000"
    assert network.normalize_endpoint("https://example.com", allow_private=False) == "https://example.com"


def test_a_card_is_verified_before_anything_is_stored() -> None:
    states, servers = build_pair(0, 0, names=("far-away", "peer"))
    try:
        endpoint = f"http://127.0.0.1:{port_of(servers[0])}"
        verified = network.verify_endpoint(endpoint)
        assert verified["card"]["name"] == "far-away"
        assert verified["kid"], "the header must carry a key id"
        assert verified["jwk"].get("x") and verified["jwk"].get("y")
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_connecting_requires_a_declared_type() -> None:
    states, servers = build_pair(0, 0, names=("typed", "peer"))
    try:
        endpoint = f"http://127.0.0.1:{port_of(servers[0])}"
        try:
            service.connect_agent({"endpoint": endpoint})
        except network.ConnectRefused as error:
            assert error.code == "kind_required"
        else:
            raise AssertionError("a connection without a declared type must be refused")

        result = service.connect_agent({"endpoint": endpoint, "kind": "supply", "city": "berlin"})
        assert result["connected"] is True
        assert result["checks"] == {"card_verified": True, "extension_answers": True}
        listed = service.remote_agents()
        assert any(agent["name"] == "typed" for agent in listed)
        assert all("endpoint" not in agent for agent in listed), (
            "a connected agent's address is not published: an address is an invitation to probe"
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_matching_across_two_processes_is_a_recorded_gap() -> None:
    """Known gap, written down as a test instead of as prose.

    The negotiation log assumes both sides share one journal ("no wire between the agents"), which
    holds when both agents are raised in one process and fails when the counterparty is a separate
    one: it refuses a turn it never saw. The fix is a witness step in the extension, where a side
    records the counterparty's signed turn after checking it against the counterparty's card. Until
    that lands, a connected agent shows up as connected and its exchange is refused by code.
    """
    states, servers = build_pair(0, 0, names=("distant-holder", "peer"))
    try:
        endpoint = f"http://127.0.0.1:{port_of(servers[0])}"
        result = network.run_remote_exchange(
            ["skill:brand-design", "city:berlin"], endpoint, 3000,
            remote_id="distant-holder", remote_kind="supply",
        )
        assert result["remote"] is True
        assert result["opened"] is False
        assert result["rejection"].startswith("remote_refused"), result["rejection"]
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
