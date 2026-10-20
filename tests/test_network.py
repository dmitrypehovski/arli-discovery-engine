"""Tests for connecting somebody else's agent, and for matching across two processes.

Why these exist. An endpoint is supplied by a stranger, so the guards that stand between our server
and that stranger's address are the most important code here. And the point of the witness step is
that two separate machines can negotiate, so the test that matters starts a real second process and
drives it over the network rather than raising both agents in one interpreter.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_TMP = tempfile.NamedTemporaryFile(prefix="arli-network-test-", suffix=".sqlite3", delete=False)
os.environ["ARLI_MATCH_DB"] = _TMP.name
os.environ["ARLI_MATCH_ALLOW_PRIVATE"] = "1"

from app import network, service  # noqa: E402
from arli_engine.serve import build_pair, port_of  # noqa: E402

service.init()
REPO = Path(__file__).resolve().parent.parent


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_for_card(port: int, timeout: float = 25.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = httpx.get(f"http://127.0.0.1:{port}/.well-known/agent-card.json", timeout=1)
            if response.status_code == 200:
                return
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    raise AssertionError(f"the agent on port {port} never came up")


@pytest.fixture()
def second_process():
    """A real second process with its own agents, used as the counterparty."""
    port_a, port_b = free_port(), free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "arli_engine.serve", "--port-a", str(port_a), "--port-b", str(port_b)],
        cwd=str(REPO), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        wait_for_card(port_b)
        yield port_a, port_b
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


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
        assert result["checks"] == {"card_verified": True, "extension_answers": True, "address_signed_our_nonce": True}
        listed = service.remote_agents()
        assert any(agent["name"] == "typed" for agent in listed)
        assert all("endpoint" not in agent for agent in listed), (
            "a connected agent's address is not published: an address is an invitation to probe"
        )
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_two_separate_processes_settle_a_pair(second_process) -> None:
    """The witness step, exercised the only way that proves it: across two processes.

    Each side keeps its own journal, so a turn one side takes is invisible to the other until it is
    witnessed. This test would have failed before the witness clause: the counterparty refused a
    turn it had never seen.
    """
    _port_a, port_b = second_process
    endpoint = f"http://127.0.0.1:{port_b}"
    with httpx.Client(timeout=10) as client:
        name = client.get(f"{endpoint}/.well-known/agent-card.json").json()["name"]

    result = network.run_remote_exchange(
        ["need:infra", "skill:design", "city:sf", "role:founder"],
        endpoint, 3000, remote_id=name, remote_kind="supply", my_kind="demand",
    )
    assert result["opened"] is True, result.get("rejection")
    assert result["agreed"] is True, "the counterparty must accept through its own journal"
    # 7500, not 5000: "need:infra" now meets "offer:infra" under the published vocabulary.
    assert result["overlap_basis_points"] == 7500, result
    assert result["matched_features"] == ["city:sf", "need:infra", "skill:design"]
    assert result["passed_floor"] is True
    assert result["wires_clean"] is True, result["leaked_features"]


def test_a_connected_agent_is_matched_through_the_service(second_process) -> None:
    _port_a, port_b = second_process
    connected = service.connect_agent({"endpoint": f"http://127.0.0.1:{port_b}", "kind": "supply"})
    assert connected["connected"] is True

    scan = service.scan_room(["need:infra", "skill:design", "city:sf", "role:founder"], 3000, "demand")
    remote_rows = [row for row in scan["rows"] if row.get("remote")]
    assert remote_rows, "the connected agent must appear in the scan"
    found = [row for row in remote_rows if row.get("passed_floor")]
    assert found, "the connected agent shares two features and must clear the floor of 3000"
    assert found[0]["matched_features"] == ["city:sf", "need:infra", "skill:design"]
    assert scan["wires_clean"] is True, [row.get("leaked_features") for row in scan["rows"]]


def test_connecting_says_which_refusal_the_agent_gave(monkeypatch) -> None:
    """A host who is told "did not answer" looks in the wrong place.

    The agent's own refusal code means something specific, so it travels: here the agent has already
    signed a nonce for this address and purpose, and the connect says so instead of blaming the port.
    """
    from arli_engine.client import AgentClient
    from arli_engine.serve import build_pair, port_of
    from app.network import ConnectRefused
    from app.service import connect_agent

    monkeypatch.setenv("ARLI_MATCH_ALLOW_PRIVATE", "1")
    _states, servers = build_pair(0, 0, names=("signer", "peer"))
    try:
        endpoint = f"http://127.0.0.1:{port_of(servers[0])}"
        with AgentClient(endpoint) as client:
            client.call("arli/identity/sign", {"nonce": "a-nonce-long-enough-to-connect-1",
                                               "purpose": "connect"})
        with pytest.raises(ConnectRefused) as refusal:
            connect_agent({"endpoint": endpoint, "kind": "supply"})
        assert refusal.value.code == "rate_limited", refusal.value
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
