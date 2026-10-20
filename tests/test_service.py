"""Tests for ARLI Match as a service: storage, registration, and a scan over stored agents.

Why these exist. A service makes promises a demonstration does not: your agent is still there next
time, your feature list is never returned, and a scan runs against the agents actually in the room.
Each promise gets a test that fails when it stops being true. The database is a throwaway file.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The store path is read when the module loads, so it must be set before the import.
_TMP = tempfile.NamedTemporaryFile(prefix="arli-match-test-", suffix=".sqlite3", delete=False)
os.environ["ARLI_MATCH_DB"] = _TMP.name

from app import service  # noqa: E402

service.init()

GOOD = {
    "name": "Test Studio", "kind": "supply", "city": "berlin", "headline": "brand studio",
    "will_not_publish": "client list",
    "features": ["skill:brand-design", "city:berlin", "industry:retail", "need:infra"],
}


def test_registration_stores_the_agent_and_returns_a_key() -> None:
    agent, token = service.create_agent(dict(GOOD, name="Registered One"))
    assert agent["id"].startswith("a-")
    assert agent["feature_count"] == 4
    assert len(token) > 20, "the token is the only key to the agent"
    assert service.owned_agent(agent["id"], token) is not None
    assert service.owned_agent(agent["id"], "wrong-token") is None


def test_registration_refuses_empty_or_absurd_lists() -> None:
    for broken, expected in (
        ({**GOOD, "features": []}, "at least one feature"),
        ({**GOOD, "kind": "partner"}, "kind must be"),
        ({**GOOD, "name": "   "}, "name is required"),
        ({**GOOD, "features": [f"f:{index}" for index in range(service.MAX_FEATURES + 1)]}, "cap"),
    ):
        try:
            service.create_agent(broken)
        except ValueError as error:
            assert expected in str(error), (expected, str(error))
        else:
            raise AssertionError(f"accepted a broken registration: {expected}")


def test_the_room_never_publishes_a_feature_list() -> None:
    service.create_agent(dict(GOOD, name="Registered Two"))
    room = service.room_view()
    stored = [agent for agent in room if not agent.get("sample") and not agent.get("remote")]
    assert stored, "the registered agent must appear in the room"
    blob = " ".join(str(value) for agent in stored for value in agent.values())
    for feature in GOOD["features"]:
        assert feature not in blob, f"the room published {feature}"
    for agent in stored:
        assert "features" not in agent
        assert agent["feature_count"] == 4


def test_a_scan_runs_against_stored_agents_and_finds_them() -> None:
    service.create_agent(dict(GOOD, name="Registered Three"))
    result = service.scan_room(["need:brand-design", "city:berlin", "industry:retail"], 3000, "demand")
    assert result["registered_agents"] >= 1
    assert result["wires_clean"] is True, "a feature reached a wire"
    assert result["agents_checked"] == len(result["rows"])

    stored_rows = [row for row in result["rows"] if not row.get("sample")]
    assert stored_rows, "the registered agents must be scanned"
    found = [row for row in stored_rows if row.get("passed_floor")]
    assert found, "a registered agent with three shared features must clear the floor of 3000"
    assert found[0]["matched_features"]


def test_an_exchange_can_be_driven_for_a_stored_agent() -> None:
    agent, _ = service.create_agent(dict(GOOD, name="Registered Four", kind="supply"))
    entry = service.entry_for(agent["id"])
    assert entry is not None and entry["features"], "the exchange needs the stored list"
    assert entry["sample"] is False
    assert service.entry_for("nobody") is None


def test_a_buyer_does_not_pair_with_another_buyer() -> None:
    result = service.scan_room(["need:brand-design", "city:berlin"], 1000, "demand")
    buyers = [row for row in result["rows"] if row["kind"] == "demand"]
    assert buyers, "the room holds buyers, so this test means something"
    assert all(not row["opened"] for row in buyers)
    assert all(row["rejection"] == "types_not_comparable" for row in buyers)


def test_only_the_owner_key_can_change_an_agent() -> None:
    agent, token = service.create_agent(dict(GOOD, name="Registered Five"))
    assert service.update_agent(agent["id"], "not-my-token", {"headline": "hijack"}) is None
    assert service.update_agent(agent["id"], token, {"headline": "mine now"})["headline"] == "mine now"

    changed = service.update_agent(agent["id"], token,
                                   {"features": ["skill:brand-design", "city:berlin", "need:infra"]})
    assert len(changed["features"]) == 3, "the owner can replace the list"
    assert service.stored_agents(include_features=True)
    stored = [row for row in service.stored_agents(include_features=True) if row["id"] == agent["id"]][0]
    assert stored["feature_count"] == 3


def test_a_broken_change_is_refused_without_touching_the_list() -> None:
    agent, token = service.create_agent(dict(GOOD, name="Registered Six"))
    for broken, expected in (
        ({"features": []}, "at least one feature"),
        ({"kind": "partner"}, "kind must be"),
        ({"features": [f"f:{index}" for index in range(service.MAX_FEATURES + 1)]}, "cap"),
    ):
        try:
            service.update_agent(agent["id"], token, broken)
        except ValueError as error:
            assert expected in str(error), (expected, str(error))
        else:
            raise AssertionError(f"accepted a broken change: {expected}")
    kept = service.owned_agent(agent["id"], token)
    assert len(kept["features"]) == 4, "a refused change must leave the list alone"


def test_a_removed_agent_leaves_the_room() -> None:
    agent, token = service.create_agent(dict(GOOD, name="Registered Seven"))
    assert any(row["id"] == agent["id"] for row in service.stored_agents())
    with service.connect() as connection:
        connection.execute("UPDATE agents SET active = 0 WHERE id = ? AND owner_token = ?",
                           (agent["id"], token))
    assert not any(row["id"] == agent["id"] for row in service.stored_agents())
    assert service.entry_for(agent["id"]) is None, "a removed agent must not be matchable"


def test_a_deleted_store_is_recreated_by_a_request() -> None:
    """The store can vanish under a running service; a request must not be what discovers it."""
    keep = service.DB_PATH
    try:
        service.DB_PATH = keep.with_suffix(".vanished.sqlite3")
        if service.DB_PATH.exists():
            service.DB_PATH.unlink()
        assert service.stored_agents() == [], "a missing table must read as an empty room"
        agent, token = service.create_agent(dict(GOOD, name="Registered After Wipe"))
        assert service.owned_agent(agent["id"], token) is not None
    finally:
        if service.DB_PATH.exists():
            service.DB_PATH.unlink()
        service.DB_PATH = keep

def test_the_page_offers_a_way_to_connect_an_agent_by_address() -> None:
    """A facility nobody can find is not a facility: the form and its reasons have to be on the page.

    The room can connect an agent that runs elsewhere, and that is the whole point of a network
    rather than a form. So the page carries the address field, the button, the three checks and a
    plain reading for each refusal code a host can actually hit.
    """
    page = (Path(__file__).resolve().parent.parent / "app" / "home.html").read_text(encoding="utf-8")
    for needed in ('id="cEndpoint"', 'id="connBtn"', 'id="connChecks"', "connectReasons"):
        assert needed in page, f"the page must offer {needed}"
    for code in ("address_not_reachable", "card_not_verified", "agent_not_answering",
                 "address_key_mismatch"):
        assert code in page, f"a host hitting {code} must be able to read what it means"
    assert 'href="#connect"' in page, "the connect section has to be reachable from the navigation"


def test_the_three_moves_block_uses_the_card_markup_it_is_styled_for() -> None:
    """Mixing two markup patterns breaks the layout silently, and only a browser notices.

    The page has two shapes for a row of steps: `.three` (equal cards, three across) and `.steps`
    with `.step` (a numbered circle beside a body, used for the connect recipe). A `.three` child
    inside `.steps` inherits a two-column grid and the block flows apart. Anyone can do that again by
    hand, so the shape is asserted here.
    """
    page = (Path(__file__).resolve().parent.parent / "app" / "home.html").read_text(encoding="utf-8")
    how = page[page.index('<section id="how">'):page.index('<section id="demo">')]
    assert 'class="three"' in how, "the three moves block uses the equal-card row"
    assert 'class="steps' not in how and 'class="step ' not in how, (
        "the unit-and-body pattern does not belong in this block"
    )
    assert how.count("<span class=\"n\">") == 3, "three numbered cards"
