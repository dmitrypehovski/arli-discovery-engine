"""One exchange opened directly at another agent's address, with no room involved.

The room is a place where strangers find each other. This is what two sides do when they already know
each other's addresses, and the claim that "nothing is in the middle" should hold for the session and not
only for the data.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arli_engine.adapters.a2a.server import start_agent  # noqa: E402
from arli_engine.agent import build_agent  # noqa: E402
from arli_engine.exchange import direct_exchange  # noqa: E402


@pytest.fixture()
def peer():
    """A real agent, on a real port, holding its own list."""
    # A card that is not signed is worth nothing, and this exchange checks it before the first call.
    from arli_engine.core.signing import KeyPair

    key = KeyPair.generate()
    state = build_agent("peer-holder", ["offer:warehouse", "city:shenzhen", "capacity:3-teams"],
                        "http://127.0.0.1:0", private_pem=key.private_pem)
    server, _thread = start_agent(state, 0, host="127.0.0.1")
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_a_direct_exchange_settles_and_matches_across_the_vocabulary(peer: str) -> None:
    """No room, no service: one side opens at the other's address and both learn the intersection."""
    result = direct_exchange(peer, ["need:warehouse", "city:shenzhen"], floor=3000,
                             my_kind="demand", peer_kind="supply", my_name="buyer")
    assert result["opened"] is True, result
    assert result["rejection"] is None
    assert result["agreed"] is True, "the counterparty must accept through its own journal"
    assert result["matched_features"] == ["city:shenzhen", "need:warehouse"], result
    assert result["overlap_basis_points"] == 10000
    assert result["passed_floor"] is True


def test_the_two_sides_report_their_own_spelling_of_the_same_match(peer: str) -> None:
    """We wrote need:warehouse, they wrote offer:warehouse: one token, two strings."""
    result = direct_exchange(peer, ["need:warehouse"], floor=1000, my_name="buyer")
    assert result["matched_features"] == ["need:warehouse"], result
    assert result["their_side"]["overlap_basis_points"] == 3333, result["their_side"]


def test_a_pair_that_cannot_be_a_pair_is_refused_before_any_turn(peer: str) -> None:
    """Two offers are competitors, not a pair, and the refusal is the protocol's own code."""
    result = direct_exchange(peer, ["offer:warehouse"], floor=1000, my_kind="supply",
                             peer_kind="supply", my_name="other-supplier")
    assert result["opened"] is False
    assert result["rejection"] == "types_not_comparable", result


def test_an_empty_list_and_a_bad_floor_are_refused_locally(peer: str) -> None:
    """The same bounds as everywhere else, refused before a single call leaves this machine."""
    with pytest.raises(ValueError):
        direct_exchange(peer, [], floor=3000)
    with pytest.raises(ValueError):
        direct_exchange(peer, ["need:warehouse"], floor=-1)
    with pytest.raises(ValueError):
        direct_exchange(peer, ["need:warehouse"], floor=10001)
    with pytest.raises(ValueError):
        direct_exchange(peer, [f"need:item-{index}" for index in range(65)], floor=1000)