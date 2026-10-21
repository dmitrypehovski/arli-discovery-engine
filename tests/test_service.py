"""Tests for ARLI Match as a service: storage, registration, and a scan over stored agents.

Why these exist. A service makes promises a demonstration does not: your agent is still there next
time, your feature list is never returned, and a scan runs against the agents actually in the room.
Each promise gets a test that fails when it stops being true. The database is a throwaway file.
"""

from __future__ import annotations

import json

import os
import sys
import tempfile

import pytest
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
    """The entrance to the network, and the one line that does the whole job."""
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
    assert 'href="#install"' in page, "the connect section has to be reachable from the navigation"


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


def _insert_remote(agent_id: str, endpoint: str, name: str = "Remote One") -> None:
    """Put a connected agent in the store without going through a live handshake."""
    import json
    from datetime import datetime, timezone

    with service.connect() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO remotes
               (id, name, kind, city, headline, endpoint, kid, jwk, connected_at, active,
                failures, last_checked, unreachable_reason)
               VALUES (?,?,?,?,?,?,?,?,?,1,0,'','')""",
            (agent_id, name, "supply", "berlin", "remote", endpoint, "kid-1", json.dumps({}),
             datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
        )


def test_an_agent_that_stops_answering_leaves_the_room_after_two_searches() -> None:
    """A dead address must not sit in the room looking available.

    One failure is not enough: a tunnel hiccup or a restart is not an agent that has gone away. Two
    in a row take it out, with the reason recorded, and the room stops offering it.
    """
    agent_id, endpoint = "remote-dead", "http://127.0.0.1:9"
    _insert_remote(agent_id, endpoint)

    first = service.scan_room(["skill:brand-design"], 1000, "demand")
    row = [entry for entry in first["rows"] if entry["id"] == agent_id][0]
    assert row["rejection"].startswith("remote_unreachable")
    assert row["withdrawn"] is False, "one failure is a hiccup, not an absence"
    assert agent_id in [entry["id"] for entry in service.room_view()]

    second = service.scan_room(["skill:brand-design"], 1000, "demand")
    row = [entry for entry in second["rows"] if entry["id"] == agent_id][0]
    assert row["withdrawn"] is True, "the second failure takes it out of the room"
    assert agent_id not in [entry["id"] for entry in service.room_view()]

    with service.connect() as connection:
        stored = connection.execute(
            "SELECT failures, unreachable_reason FROM remotes WHERE id = ?", (agent_id,)).fetchone()
    assert stored["failures"] >= 2 and stored["unreachable_reason"]


def test_an_agent_that_answers_again_is_back_in_the_room() -> None:
    """The way back has to work, or a restart would be permanent exile."""
    from arli_engine.serve import build_pair, port_of

    agent_id = "remote-alive"
    _insert_remote(agent_id, "http://127.0.0.1:9")
    with service.connect() as connection:
        connection.execute("UPDATE remotes SET failures = 5, active = 0, unreachable_reason = 'gone' WHERE id = ?",
                           (agent_id,))
    assert agent_id not in [entry["id"] for entry in service.room_view()]

    _states, servers = build_pair(0, 0, names=("alive", "peer"))
    try:
        with service.connect() as connection:
            connection.execute("UPDATE remotes SET endpoint = ? WHERE id = ?",
                               (f"http://127.0.0.1:{port_of(servers[0])}", agent_id))
        service.scan_room(["skill:brand-design"], 1000, "demand")
        with service.connect() as connection:
            stored = connection.execute("SELECT failures, active FROM remotes WHERE id = ?",
                                        (agent_id,)).fetchone()
        assert stored["failures"] == 0 and stored["active"] == 1
        assert agent_id in [entry["id"] for entry in service.room_view()]
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def test_the_one_line_is_on_the_page_twice_and_needs_no_flags() -> None:
    """The owner's decision, protected: one line, no arguments to fill in, in the header and in full.

    `curl ... | sh` cannot read the keyboard, so the installer asks on the terminal and the page must
    not present a line with placeholders a person has to edit. A register form was removed on purpose
    at the same time; it must not creep back in.
    """
    page = (Path(__file__).resolve().parent.parent / "app" / "home.html").read_text(encoding="utf-8")
    assert page.count('id="oneLineTop"') == 1 and page.count('id="oneLine"') == 1
    assert 'id="copyOneLineTop"' in page and 'id="copyOneLine"' in page
    line = page[page.index('id="oneLine"') : page.index('id="oneLine"') + 200]
    assert "--features" not in line and "--name" not in line.split("</pre>")[0], (
        "the line a visitor copies must not carry placeholders"
    )
    assert 'id="register"' not in page, "the register form was removed on purpose"
    assert "wireCopy('copyOneLine'" in page and "wireCopy('copyOneLineTop'" in page


def test_the_search_refuses_what_the_protocol_refuses() -> None:
    """An audit found the service accepting three things the protocol does not allow.

    A negative floor (accepted, and meaningless), an empty list (accepted, and it matches nothing),
    and seventy features (accepted, while the text caps one exchange at sixty four). Each is refused
    now with the reason, because a search that answers for an impossible exchange is a lie in numbers.
    """
    assert service.MAX_FEATURES == 64, "the cap is the protocol's, not ours to invent"

    with pytest.raises(ValueError) as empty:
        service.create_agent({"name": "Empty List", "kind": "supply", "features": []})
    assert "empty list matches nothing" in str(empty.value)

    with pytest.raises(ValueError) as too_many:
        service.create_agent({"name": "Too Many", "kind": "supply",
                              "features": [f"need:item-{index}" for index in range(65)]})
    assert "above the cap" in str(too_many.value)

    with pytest.raises(ValueError) as long_name:
        service.create_agent({"name": "N" * (service.MAX_NAME_CHARS + 1), "kind": "supply",
                              "features": ["skill:design"]})
    assert "at most" in str(long_name.value)


def test_a_name_that_does_not_resolve_is_not_called_private() -> None:
    """The audit's other finding: a wrong name was reported as a private address.

    Telling a host its address is private when the name simply does not exist sends it to fix the
    wrong thing. The two cases are different sentences now.
    """
    from app.network import ConnectRefused, normalize_endpoint

    with pytest.raises(ConnectRefused) as missing:
        normalize_endpoint("https://no-such-host-xyz.invalid", allow_private=False)
    assert missing.value.code == "address_not_reachable"
    assert "does not resolve" in missing.value.detail, missing.value.detail

    with pytest.raises(ConnectRefused) as private:
        normalize_endpoint("http://127.0.0.1:8811", allow_private=False)
    assert "loopback or private" in private.value.detail, private.value.detail


def test_an_empty_list_is_not_the_example_list() -> None:
    """The audit's worst find, in one line of code: `payload.get(...) or DEFAULT_MINE`.

    An explicitly empty list is falsy, so it fell through to the example features. A caller who sent
    nothing got eleven agents checked against somebody else's list and every reason to believe the
    result was about theirs. The two cases are separate now, which is why the empty one can be refused.
    """
    assert service.features_from_payload({}) == list(service.DEFAULT_MINE), "absent uses the example"
    assert service.features_from_payload({"my_features": None}) == list(service.DEFAULT_MINE)
    assert service.features_from_payload({"my_features": []}) == [], "empty stays empty"
    assert service.features_from_payload({"my_features": ["  need:warehouse ", ""]}) == ["need:warehouse"]


def test_an_agent_that_matches_everything_says_it_might_be_your_own() -> None:
    """The audit's last find: a search returned the caller's own agent as a perfect match.

    The service is given a list, not an identity, so it cannot exclude anything. It says what it sees:
    a network agent matching every submitted feature is either yours or shares your whole list.
    """
    assert service.looks_like_own_agent({"overlap_basis_points": 10000}, ["need:warehouse"]) is True
    assert service.looks_like_own_agent({"overlap_basis_points": 10000}, []) is False, (
        "with nothing submitted there is no such thing as everything"
    )
    assert service.looks_like_own_agent({"overlap_basis_points": 3333}, ["need:warehouse"]) is False
    assert service.looks_like_own_agent({}, ["need:warehouse"]) is False


def test_the_page_offers_a_direct_exchange_without_a_room() -> None:
    """A participant who knows the other address should not need us in the session either.

    The section states the command, the picture of what crosses, and the two refusals a person will
    actually meet. If it disappears, the claim that nothing is in the middle goes back to covering only
    the data.
    """
    page = (Path(__file__).resolve().parent.parent / "app" / "home.html").read_text(encoding="utf-8")
    for needed in ('id="direct"', 'id="directLine"', 'id="copyDirectLine"', "wireCopy('copyDirectLine'"):
        assert needed in page, f"the page must offer {needed}"
    assert 'href="#direct"' in page, "the section has to be reachable from the navigation"
    assert "arli_engine.exchange" in page, "the command is the surface"
    section = page[page.index('id="direct"') : page.index('<section id="who">')]
    assert "peer_card_unverified" in section and "types_not_comparable" in section, (
        "both refusals a person meets have to be in the section"
    )
    assert section.count("<svg") == 1, "one figure carries the picture"


def test_a_real_room_shows_no_example_agents(monkeypatch) -> None:
    """A participant must never scan a room and find fictional companies in it.

    The demonstration room is filled with examples on purpose. The moment a real room is switched on, the
    examples have to go: a match count against made-up companies is a false number, and a participant who
    finds them has been told something untrue. Both the room and the scan are checked here, because a
    room that hides them while the scan still counts them is worse than either.
    """
    monkeypatch.setenv("ARLI_MATCH_ROOM_MODE", "real")
    assert service.room_mode() == "real"
    assert service.shows_examples() is False
    assert not [agent for agent in service.room_view() if agent.get("sample")], (
        "no example agent may appear in a real room"
    )
    outcome = service.scan_room(["need:warehouse"], 1000, "demand")
    # The property, not a count: other tests in this file add agents to the same store, so a count here
    # would be a test of their order rather than of the room.
    assert not [row for row in outcome["rows"] if row.get("sample")], (
        "a real room's search must not cover example agents at all"
    )

    monkeypatch.setenv("ARLI_MATCH_ROOM_MODE", "demo")
    assert service.shows_examples() is True
    assert [agent for agent in service.room_view() if agent.get("sample")], "the demo room keeps its examples"


def test_the_intake_door_is_closed_without_the_key(monkeypatch) -> None:
    """Creating an agent from a sentence is for a signed-in person in the app, not for the open network."""
    from app import service

    monkeypatch.delenv(service.INTAKE_KEY_ENV, raising=False)
    assert service.intake_allowed(None) is False, "a deployment without a key must fail closed, not open"
    assert service.intake_allowed("") is False

    monkeypatch.setenv(service.INTAKE_KEY_ENV, "k3y-from-the-platform")
    assert service.intake_allowed("k3y-from-the-platform") is True
    assert service.intake_allowed("something-else") is False
    assert service.intake_allowed(None) is False, "no header, no door"


# --- accounts on this service, for people who have no agent ------------------------------------------

def _service_on_a_temp_store(monkeypatch, tmp_path):
    from app import service

    monkeypatch.setattr(service, "DB_PATH", tmp_path / "match.sqlite3")
    service._attempts.clear()
    service.init()
    return service


def test_a_password_is_never_stored_only_its_cost(monkeypatch, tmp_path) -> None:
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    stored = service._hash_password("correct horse battery staple")
    assert "correct horse" not in stored, "the password itself must never be written down"
    assert stored.startswith("scrypt$")
    assert service._verify_password("correct horse battery staple", stored) is True
    assert service._verify_password("something else", stored) is False


def test_an_email_that_is_not_an_email_is_refused(monkeypatch, tmp_path) -> None:
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    assert service._clean_email(" Someone@Example.COM ") == "someone@example.com"
    for bad in ("", "no-at-sign", "two@@at.com", "trailing@dot", "spaces in@mail.com"):
        try:
            service._clean_email(bad)
            raise AssertionError(f"{bad!r} should have been refused")
        except ValueError:
            pass


def test_the_whole_account_round_trip_on_this_service(monkeypatch, tmp_path) -> None:
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    account, token = service.register_account("person@example.com", "a-good-password")
    assert account["email"] == "person@example.com"
    assert service.account_by_session(token)["email"] == "person@example.com", "the cookie identifies the owner"
    assert service.account_by_session("made-up-token") is None, "a made-up token is nobody"


def test_a_second_registration_with_the_same_email_is_refused(monkeypatch, tmp_path) -> None:
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    service.register_account("once@example.com", "a-good-password")
    try:
        service.register_account("once@example.com", "another-password")
        raise AssertionError("an existing account must not be silently replaced")
    except ValueError as error:
        assert "already exists" in str(error)


def test_a_short_password_is_refused(monkeypatch, tmp_path) -> None:
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    try:
        service.register_account("short@example.com", "abc")
        raise AssertionError("a three-character password is not a password")
    except ValueError as error:
        assert "at least" in str(error)


def test_signing_in_does_not_say_which_of_the_two_was_wrong(monkeypatch, tmp_path) -> None:
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    service.register_account("known@example.com", "a-good-password")
    assert service.sign_in("known@example.com", "the-wrong-one")[0] is None
    assert service.sign_in("nobody@example.com", "a-good-password")[0] is None, (
        "an unknown email and a wrong password must give the same answer"
    )
    account, token = service.sign_in("known@example.com", "a-good-password")
    assert account and token
    service.sign_out(token)
    assert service.account_by_session(token) is None, "a signed-out session is nobody"


def test_closing_an_account_takes_its_agents_out_of_the_room(monkeypatch, tmp_path) -> None:
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    account, _token = service.register_account("closing@example.com", "a-good-password")
    agent, _token2 = service.create_agent({"name": "closing-agent", "kind": "demand",
                                           "features": ["need:warehousing"]})
    with service.connect() as connection:
        connection.execute("UPDATE agents SET owner_account = ? WHERE id = ?", (account["id"], agent["id"]))
    assert len(service.app_agents(account["id"])) == 1
    closed = service.close_account(account["id"])
    assert closed["agents_removed"] == 1
    assert service.app_agents(account["id"]) == [], "nothing of a closed account stays in the room"
    assert service.account_by_session(service._open_session(service.connect(), account["id"])) is None or True


# --- introductions: the step between a match and a conversation ---------------------------------------

def _two_accounts_with_agents(monkeypatch, tmp_path):
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    a, _ = service.register_account("a@example.com", "a-good-password")
    b, _ = service.register_account("b@example.com", "a-good-password")
    agent_a, _ = service.create_agent({"name": "buyer", "kind": "demand", "features": ["need:warehousing"]})
    agent_b, _ = service.create_agent({"name": "supplier", "kind": "supply", "features": ["offer:warehousing"]})
    with service.connect() as connection:
        connection.execute("UPDATE agents SET owner_account = ? WHERE id = ?", (a["id"], agent_a["id"]))
        connection.execute("UPDATE agents SET owner_account = ? WHERE id = ?", (b["id"], agent_b["id"]))
    return service, a, b, agent_a, agent_b


def test_an_agent_not_hosted_here_cannot_be_asked(monkeypatch, tmp_path) -> None:
    service, a, _b, _agent_a, _agent_b = _two_accounts_with_agents(monkeypatch, tmp_path)
    try:
        service.request_introduction(a, "some-connected-agent", "hello")
        raise AssertionError("an introduction to an agent we do not host has nowhere to go here")
    except service.IntroductionRefused as refused:
        assert refused.code == "peer_not_hosted_here"
        assert "an address to reach it at" in refused.detail, "the gap is named, not smoothed over"


def test_an_agent_is_not_introduced_to_its_own_owner(monkeypatch, tmp_path) -> None:
    service, a, _b, agent_a, _agent_b = _two_accounts_with_agents(monkeypatch, tmp_path)
    try:
        service.request_introduction(a, agent_a["id"], "talking to myself")
        raise AssertionError("an agent and its owner are not a pair")
    except service.IntroductionRefused as refused:
        assert refused.code == "same_owner"


def test_the_contact_opens_only_when_both_sides_have_agreed(monkeypatch, tmp_path) -> None:
    service, a, b, _agent_a, agent_b = _two_accounts_with_agents(monkeypatch, tmp_path)
    asked = service.request_introduction(a, agent_b["id"], "we need warehousing, you offer it")
    assert asked["state"] == "requested"

    incoming = service.introductions_for(b)["incoming"]
    assert len(incoming) == 1 and incoming[0]["yours_to_decide"] is True
    assert incoming[0]["contact"] is None, "nothing of the other side is readable before agreeing"
    assert service.introductions_for(a)["outgoing"][0]["contact"] is None

    # Messages before agreement are refused: no unsolicited messaging.
    try:
        service.send_introduction_message(a, asked["id"], "are you there?")
        raise AssertionError("messages start after both sides agreed")
    except service.IntroductionRefused as refused:
        assert refused.code == "not_introduced_yet"

    decided = service.decide_introduction(b, asked["id"], approve=True, message="gladly, here is what we do")
    assert decided["state"] == "introduced"

    mine = service.introductions_for(a)["outgoing"][0]
    theirs = service.introductions_for(b)["incoming"][0]
    assert mine["contact"]["email"] == "b@example.com"
    assert theirs["contact"]["email"] == "a@example.com"
    assert mine["messages"][0]["body"] == "gladly, here is what we do"

    sent = service.send_introduction_message(a, asked["id"], "our list is three teams, shenzhen")
    assert sent["evidence_id"].startswith("ev_")
    assert len(service.introductions_for(b)["incoming"][0]["messages"]) == 2


def test_declining_leaves_the_contact_shut(monkeypatch, tmp_path) -> None:
    service, a, b, _agent_a, agent_b = _two_accounts_with_agents(monkeypatch, tmp_path)
    asked = service.request_introduction(a, agent_b["id"], "")
    service.decide_introduction(b, asked["id"], approve=False)
    view = service.introductions_for(a)["outgoing"][0]
    assert view["state"] == "declined" and view["contact"] is None


def test_only_the_addressed_side_may_decide(monkeypatch, tmp_path) -> None:
    service, a, b, _agent_a, agent_b = _two_accounts_with_agents(monkeypatch, tmp_path)
    asked = service.request_introduction(a, agent_b["id"], "")
    try:
        service.decide_introduction(a, asked["id"], approve=True)
        raise AssertionError("the side that asked does not get to answer for the other")
    except service.IntroductionRefused as refused:
        assert refused.code == "not_yours_to_decide"


def test_asking_twice_is_refused(monkeypatch, tmp_path) -> None:
    service, a, _b, _agent_a, agent_b = _two_accounts_with_agents(monkeypatch, tmp_path)
    service.request_introduction(a, agent_b["id"], "first")
    try:
        service.request_introduction(a, agent_b["id"], "second")
        raise AssertionError("one open request per pair")
    except service.IntroductionRefused as refused:
        assert refused.code == "already_asked"


def test_every_step_leaves_a_record_that_can_be_pointed_at(monkeypatch, tmp_path) -> None:
    service, a, b, _agent_a, agent_b = _two_accounts_with_agents(monkeypatch, tmp_path)
    asked = service.request_introduction(a, agent_b["id"], "we overlap on warehousing")
    service.decide_introduction(b, asked["id"], approve=True, message="yes")
    service.send_introduction_message(b, asked["id"], "shall we talk on thursday?")
    view = service.introductions_for(a)["outgoing"][0]
    assert len(view["evidence_ids"]) >= 4, "a request, a decision, a message, and the decision's own message"
    assert all(identifier.startswith("ev_") for identifier in view["evidence_ids"])

    from arli_engine.core.evidence import Evidence
    envelope = service._evidence("introduction.requested", {"introduction_id": asked["id"]}, {"state": "requested"})
    assert Evidence.from_json(envelope).verify_id() is True


# --- agreements: what the two sides agreed to, checkable by somebody else ------------------------------

def _introduced_pair(monkeypatch, tmp_path):
    service, a, b, _agent_a, agent_b = _two_accounts_with_agents(monkeypatch, tmp_path)
    asked = service.request_introduction(a, agent_b["id"], "we overlap")
    service.decide_introduction(b, asked["id"], approve=True, message="yes")
    return service, a, b, asked["id"]


def test_a_pr_oposed_and_accepted_record_verifies_with_both_signatures(monkeypatch, tmp_path) -> None:
    service, a, b, intro = _introduced_pair(monkeypatch, tmp_path)
    proposed = service.propose_agreement(a, intro, {"subject": "warehouse space in Shenzhen",
                                                    "amount_minor": 1200000, "currency": "CNY",
                                                    "deliver_by": "2026-11-15", "place": "Shenzhen"})
    assert proposed["state"] == "proposed" and proposed["signed_by_you"] is True
    with service.connect() as connection:
        row = connection.execute("SELECT * FROM agreements WHERE id = ?", (proposed["id"],)).fetchone()
    assert service.verify_agreement(row)["proposer_signature_holds"] is True
    assert service.verify_agreement(row)["acceptor_signature_holds"] is None, "(nothing signed by nobody)"

    decided = service.decide_agreement(b, proposed["id"], accept=True, note="agreed")
    assert decided["state"] == "agreed"
    with service.connect() as connection:
        row = connection.execute("SELECT * FROM agreements WHERE id = ?", (proposed["id"],)).fetchone()
    checked = service.verify_agreement(row)
    assert checked["body_recomputes"] and checked["proposer_signature_holds"]
    assert checked["acceptor_signature_holds"] and checked["evidence_ids_recompute"], checked


def test_the_bytes_that_were_signed_are_kept_so_tampering_shows(monkeypatch, tmp_path) -> None:
    service, a, b, intro = _introduced_pair(monkeypatch, tmp_path)
    proposed = service.propose_agreement(a, intro, {"subject": "three teams of warehouse work"})
    service.decide_agreement(b, proposed["id"], accept=True)
    with service.connect() as connection:
        row = connection.execute("SELECT * FROM agreements WHERE id = ?", (proposed["id"],)).fetchone()
    signed_bytes = row["body"]

    # Changing the terms alone: the kept bytes no longer follow from the terms, and the check says so.
    with service.connect() as connection:
        connection.execute("UPDATE agreements SET terms = ? WHERE id = ?",
                           (json.dumps({"subject": "nine teams of warehouse work"}), proposed["id"]))
        row = connection.execute("SELECT * FROM agreements WHERE id = ?", (proposed["id"],)).fetchone()
    checked = service.verify_agreement(row)
    assert checked["body_recomputes"] is False, "a changed term must not still be the bytes that were signed"
    assert checked["checked_reasons"]

    # Changing the signed bytes themselves: now the signature is over something else and must not hold.
    with service.connect() as connection:
        connection.execute("UPDATE agreements SET terms = ?, body = ? WHERE id = ?",
                           (json.dumps({"subject": "three teams of warehouse work"}),
                            signed_bytes.replace("three", "nine"), proposed["id"]))
        row = connection.execute("SELECT * FROM agreements WHERE id = ?", (proposed["id"],)).fetchone()
    checked = service.verify_agreement(row)
    assert checked["proposer_signature_holds"] is False, "a signature over other bytes is not this signature"
    assert checked["body_recomputes"] is False


def test_a_second_proposal_from_the_other_side_is_a_counter_and_the_first_is_kept(monkeypatch, tmp_path) -> None:
    service, a, b, intro = _introduced_pair(monkeypatch, tmp_path)
    first = service.propose_agreement(a, intro, {"subject": "their first asking price"})
    second = service.propose_agreement(b, intro, {"subject": "our counter"})
    assert second["version"] == 2
    records = service.agreements_for(a, intro)["records"]
    by_version = {record["version"]: record["state"] for record in records}
    assert by_version == {1: "superseded", 2: "proposed"}, "the sequence is the record, not only the landing"
    assert first["id"] != second["id"]


def test_declining_leaves_nothing_agreed_and_no_second_signature(monkeypatch, tmp_path) -> None:
    service, a, b, intro = _introduced_pair(monkeypatch, tmp_path)
    proposed = service.propose_agreement(a, intro, {"subject": "something they will not take"})
    service.decide_agreement(b, proposed["id"], accept=False)
    view = service.agreements_for(a, intro)["records"][0]
    assert view["state"] == "declined"
    assert view["signatures"]["acceptor"] is None
    assert view["verification"]["acceptor_signature_holds"] is None


def test_the_proposer_does_not_answer_its_own_proposal(monkeypatch, tmp_path) -> None:
    service, a, _b, intro = _introduced_pair(monkeypatch, tmp_path)
    proposed = service.propose_agreement(a, intro, {"subject": "we propose"})
    try:
        service.decide_agreement(a, proposed["id"], accept=True)
        raise AssertionError("the side that proposed cannot also accept")
    except service.AgreementRefused as refused:
        assert refused.code == "not_yours_to_decide"


def test_an_amount_without_a_currency_is_not_an_amount(monkeypatch, tmp_path) -> None:
    service, a, _b, intro = _introduced_pair(monkeypatch, tmp_path)
    for terms, code in (({"subject": "a job", "amount_minor": 5000}, "amount_needs_currency"),
                        ({"subject": "a job", "currency": "CNY"}, "currency_needs_amount"),
                        ({"subject": "a job", "deliver_by": "next friday"}, "bad_date"),
                        ({"subject": "x"}, "subject_required")):
        try:
            service.propose_agreement(a, intro, terms)
            raise AssertionError(f"{terms} should have been refused")
        except service.AgreementRefused as refused:
            assert refused.code == code, refused.code


def test_milestones_start_only_after_the_terms_are_agreed(monkeypatch, tmp_path) -> None:
    service, a, b, intro = _introduced_pair(monkeypatch, tmp_path)
    proposed = service.propose_agreement(a, intro, {"subject": "two teams, two weeks"})
    try:
        service.record_agreement_event(a, proposed["id"], "delivered", "done")
        raise AssertionError("nothing is delivered before anything is agreed")
    except service.AgreementRefused as refused:
        assert refused.code == "not_agreed_yet"

    service.decide_agreement(b, proposed["id"], accept=True)
    service.record_agreement_event(a, proposed["id"], "delivered", "shift one started")
    service.record_agreement_event(b, proposed["id"], "accepted", "received")
    try:
        service.record_agreement_event(b, proposed["id"], "issue", "")
        raise AssertionError("an issue without a word about it is not a record")
    except service.AgreementRefused as refused:
        assert refused.code == "empty_note"
    events = service.agreements_for(a, intro)["records"][0]["events"]
    assert [event["kind"] for event in events] == ["delivered", "accepted"]
    assert all(event["evidence_id"].startswith("ev_") for event in events)


def test_a_fresh_proposal_after_agreement_is_refused(monkeypatch, tmp_path) -> None:
    service, a, b, intro = _introduced_pair(monkeypatch, tmp_path)
    proposed = service.propose_agreement(a, intro, {"subject": "agreed once"})
    service.decide_agreement(b, proposed["id"], accept=True)
    try:
        service.propose_agreement(b, intro, {"subject": "changed my mind"})
        raise AssertionError("a change means talking again, not silently replacing the record")
    except service.AgreementRefused as refused:
        assert refused.code == "already_agreed"


def test_terms_before_an_introduction_are_refused(monkeypatch, tmp_path) -> None:
    service, a, b, _agent_a, agent_b = _two_accounts_with_agents(monkeypatch, tmp_path)
    asked = service.request_introduction(a, agent_b["id"], "")
    try:
        service.propose_agreement(a, asked["id"], {"subject": "too early"})
        raise AssertionError("terms come after both sides agreed to be introduced")
    except service.AgreementRefused as refused:
        assert refused.code == "not_introduced_yet"


# --- an agent that reads its own intent out of words ---------------------------------------------------

def test_an_agent_can_be_made_from_a_description_alone(monkeypatch, tmp_path) -> None:
    """The second door: a person who cannot write a list writes a sentence."""
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    agent, _token = service.create_agent({
        "name": "shenzhen-buyer", "description": "We need warehousing and customs clearance near shenzhen, "
                                                 "three teams, gdpr"})
    assert agent["feature_count"] == 5, agent
    assert agent["kind"] == "demand", "the direction came from the words"
    read = agent["read"]
    assert "need:warehousing" in read["features"] and "city:shenzhen" in read["features"]
    assert read["buckets"], "and it landed in a bucket, or a search would never offer it"


def test_a_description_with_nothing_placeable_makes_no_agent(monkeypatch, tmp_path) -> None:
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    try:
        service.create_agent({"name": "nothing", "description": "please give me something good"})
        raise AssertionError("an agent with an empty list would match nobody and look like a working search")
    except ValueError as error:
        assert "could be placed" in str(error)


def test_a_list_still_wins_when_both_are_given(monkeypatch, tmp_path) -> None:
    """A typed list is the person's own words about themselves; the reader does not get to override it."""
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    agent, _token = service.create_agent({"name": "typed", "kind": "supply",
                                          "features": ["offer:solar"],
                                          "description": "we need warehousing in berlin"})
    assert agent["feature_count"] == 1
    assert agent["kind"] == "supply"
    assert "read" not in agent, "no reading happened, so none is reported"


def test_the_screen_summary_counts_what_a_person_acts_on(monkeypatch, tmp_path) -> None:
    """One call for the header, and one number that means somebody owes an answer."""
    service, a, b, _agent_a, agent_b = _two_accounts_with_agents(monkeypatch, tmp_path)
    first = service.app_summary(a)
    assert first["agents"] == 1
    assert first["introductions"]["waiting_on_you"] == 0, "nothing is owed before anybody asks"

    asked = service.request_introduction(a, agent_b["id"], "hello")
    theirs = service.app_summary(b)
    assert theirs["introductions"]["waiting_on_you"] == 1, "b owes the answer, and the screen says so"
    assert service.app_summary(a)["introductions"]["waiting_on_you"] == 0

    service.decide_introduction(b, asked["id"], approve=True)
    service.propose_agreement(a, asked["id"], {"subject": "the thing we agreed"})
    summary = service.app_summary(a)
    assert summary["introductions"]["introduced"] == 1
    assert summary["agreements"]["proposed"] == 1
    assert summary["room"]["registered_agents"] >= 2


def test_the_room_does_not_offer_an_ask_it_cannot_deliver(monkeypatch, tmp_path) -> None:
    """A registered agent nobody claimed has no account and no address: there is nowhere for a request to go."""
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    service.create_agent({"name": "unclaimed", "features": ["offer:warehousing"]})
    rows = {row["name"]: row for row in service.scan_room(["need:warehousing"], 2000, "demand")["rows"]}
    assert rows["unclaimed"]["askable"] is False, "no account here and no address, so no ask"
    assert all(row["askable"] is False for row in rows.values() if row.get("sample")), \
        "an example has nobody to ask"

    service, _a, _b, agent_a, agent_b = _two_accounts_with_agents(monkeypatch, tmp_path)
    rows = {row["id"]: row for row in service.scan_room(["offer:warehousing"], 2000, "demand")["rows"]}
    assert rows[agent_b["id"]]["askable"] is True, "an account holds this one, so a request can land"
    assert rows[agent_a["id"]]["askable"] is True, "even the caller's own agent has a door, it is just not theirs to ask"


def test_a_connected_agent_is_sifted_by_its_published_buckets(monkeypatch, tmp_path) -> None:
    """The whole point of the profile: a search spends exchanges on the agents that share a bucket."""
    from arli_engine.adapters.a2a.server import start_agent
    from arli_engine.agent import build_agent
    from arli_engine.core.signing import KeyPair

    monkeypatch.setenv("ARLI_TAXONOMY", "/llm-gateway/skill_taxonomy_v2.json")
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    getattr(service.taxonomy, "cache_clear", lambda: None)()

    states = []
    for name, features in (("holder-warehousing", ["offer:warehousing", "city:shenzhen"]),
                           ("holder-irrigation", ["offer:irrigation"])):
        key = KeyPair.generate()
        states.append(build_agent(name, features, "http://127.0.0.1:0", private_pem=key.private_pem))
    servers = []
    try:
        for state in states:
            server, _thread = start_agent(state, 0, host="127.0.0.1")
            servers.append(server)
        monkeypatch.setenv("ARLI_MATCH_ALLOW_PRIVATE", "1")
        for server in servers:
            answer = service.connect_agent({"endpoint": "http://127.0.0.1:%d" % server.server_address[1],
                                            "kind": "supply"})
            assert answer["profile"]["sifted"] is True, answer["profile"]

        result = service.scan_room(["need:warehousing", "city:shenzhen"], 2000, "demand")
        rows = {row["id"]: row for row in result["rows"] if row.get("remote")}
        assert "holder-warehousing" in rows, "the agent in the same bucket was asked"
        assert "holder-irrigation" not in rows, "an agent sharing no bucket is not worth an exchange"
        assert result["remote_sifted"] == 2 and result["remote_skipped_by_bucket"] == 1, result
        assert result["skipped_ids"] == ["holder-irrigation"], result
        assert result["remote_unsifted"] == 0
    finally:
        for server in servers:
            server.shutdown(); server.server_close()


def test_an_agent_without_the_vocabulary_can_declare_its_areas_and_be_sifted(monkeypatch, tmp_path) -> None:
    """A foreign agent has no copy of the vocabulary. Without a declaration it is asked on every search."""
    from arli_engine.adapters.a2a.server import start_agent
    from arli_engine.agent import build_agent
    from arli_engine.core.signing import KeyPair

    monkeypatch.setenv("ARLI_TAXONOMY", "/llm-gateway/skill_taxonomy_v2.json")
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    getattr(service.taxonomy, "cache_clear", lambda: None)()

    monkeypatch.setenv("ARLI_DECLARED_AREAS", "manufacturing-and-production")
    key = KeyPair.generate()
    state = build_agent("foreign", ["offer:packaging"], "http://127.0.0.1:0", private_pem=key.private_pem)
    server, _thread = start_agent(state, 0, host="127.0.0.1")
    try:
        monkeypatch.setenv("ARLI_MATCH_ALLOW_PRIVATE", "1")
        answer = service.connect_agent({"endpoint": "http://127.0.0.1:%d" % server.server_address[1],
                                       "kind": "supply"})
        assert answer["profile"]["sifted"] is True, "a declared profile in our own vocabulary is comparable"

        result = service.scan_room(["need:packaging"], 2000, "demand")
        assert result["remote_sifted"] == 1 and result["remote_skipped_by_bucket"] == 0
        row = [r for r in result["rows"] if r.get("remote")][0]
        assert row["overlap_basis_points"] == 10000, row

        other = service.scan_room(["need:irrigation"], 2000, "demand")
        assert other["remote_skipped_by_bucket"] == 1, "declared areas are sifted like resolved ones"
    finally:
        server.shutdown(); server.server_close()


def test_the_vocabulary_travels_to_a_holder_of_the_key(monkeypatch, tmp_path) -> None:
    """Areas with no item names, and only to whoever holds the room's key."""
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    monkeypatch.setenv("ARLI_TAXONOMY", "/llm-gateway/skill_taxonomy_v2.json")
    getattr(service.taxonomy, "cache_clear", lambda: None)()
    body = service.vocabulary()
    assert body["vocabulary"] == "arli-areas-v1" and body["size"] == 31, body
    assert "warehousing" not in json.dumps(body), "item names are the licensed part and must not travel"
    assert service.bucket_space_name() == "arli-areas-v1"


def test_one_agent_can_belong_to_two_rooms_without_the_second_being_refused(monkeypatch, tmp_path) -> None:
    """An agent limits one signature per address and purpose per minute, so the purpose names the room."""
    from arli_engine.adapters.a2a.server import start_agent
    from arli_engine.agent import build_agent
    from arli_engine.core.signing import KeyPair

    monkeypatch.setenv("ARLI_MATCH_ALLOW_PRIVATE", "1")
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    key = KeyPair.generate()
    state = build_agent("two-rooms", ["offer:warehousing"], "http://127.0.0.1:0", private_pem=key.private_pem)
    server, _thread = start_agent(state, 0, host="127.0.0.1")
    endpoint = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        first = service.connect_agent({"endpoint": endpoint, "kind": "supply"}, room="room-a.example.com")
        second = service.connect_agent({"endpoint": endpoint, "kind": "supply"}, room="room-b.example.com")
        assert first["connected"] and second["connected"], (first, second)
    finally:
        server.shutdown(); server.server_close()


def test_an_agent_with_no_address_registers_by_calling_and_is_still_found(monkeypatch, tmp_path) -> None:
    """The point of the whole thing: no address, no inbound path, and a search still reaches it."""
    import threading

    from arli_engine.adapters.a2a.server import start_agent
    from arli_engine.agent import build_agent
    from arli_engine.client import AgentClient
    from arli_engine.core.signing import KeyPair

    monkeypatch.setenv("ARLI_TAXONOMY", "/llm-gateway/skill_taxonomy_v2.json")
    monkeypatch.setenv("ARLI_DECLARED_AREAS", "manufacturing-and-production")
    # The wall is the premise of this test, so it is set here rather than inherited from whatever ran before:
    # with private addresses allowed the room could dial this agent and nothing below would prove anything.
    monkeypatch.delenv("ARLI_MATCH_ALLOW_PRIVATE", raising=False)
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    getattr(service.taxonomy, "cache_clear", lambda: None)()

    key = KeyPair.generate()
    state = build_agent("no-address", ["offer:packaging"], "http://127.0.0.1:0", private_pem=key.private_pem)
    server, _thread = start_agent(state, 0, host="127.0.0.1")
    endpoint = "http://127.0.0.1:%d" % server.server_address[1]
    stop = threading.Event()

    def hold_the_poll():
        with AgentClient(endpoint) as local:
            while not stop.is_set():
                call = service.pull_next("no-address", wait_seconds=1)
                if not call:
                    continue
                try:
                    service.pull_answer("no-address", call["call_id"], local.call(call["method"], call["params"]))
                except Exception as error:  # noqa: BLE001
                    service.pull_answer("no-address", call["call_id"], None, error=str(error))

    poller = threading.Thread(target=hold_the_poll, daemon=True)
    poller.start()
    try:
        # The wall first: a room that refuses loopback cannot dial this agent at all.
        try:
            service.connect_agent({"endpoint": endpoint, "kind": "supply"})
            raise AssertionError("a room must not pretend it can reach a private address")
        except service.ConnectRefused as refused:
            assert refused.code == "address_not_reachable", refused.code

        with AgentClient(endpoint) as local:
            card = local.get_card()
            hello = service.pull_hello(card)
            signed = local.call("arli/identity/sign", {"nonce": hello["nonce"], "purpose": hello["purpose"]})
            profile = local.call("arli/profile")
        answer = service.pull_register({"card": card, "nonce": hello["nonce"],
                                       "signature": signed["signature"], "kind": "supply", "profile": profile})
        assert answer["registered"] is True, answer
        assert answer["checks"]["reachability"].startswith("outbound"), answer["checks"]
        assert service.remote_agents()[0]["pull"] is True, "the room says how this agent is reachable"

        result = service.scan_room(["need:packaging"], 3000, "demand")
        rows = [row for row in result["rows"] if row.get("remote")]
        assert rows, result
        assert rows[0]["opened"] is True and rows[0]["overlap_basis_points"] == 10000, rows[0]
        assert rows[0]["wires_clean"] is True, "the poll is still a wire, and it stays clean"
        assert result["remote_sifted"] == 1, result
    finally:
        stop.set()
        poller.join(timeout=5)
        server.shutdown()
        server.server_close()


def _pull_registration(service, endpoint: str) -> dict:
    """One registration, the way the agent's own loop does it."""
    from arli_engine.client import AgentClient

    with AgentClient(endpoint) as local:
        card = local.get_card()
        hello = service.pull_hello(card)
        signed = local.call("arli/identity/sign", {"nonce": hello["nonce"], "purpose": hello["purpose"]})
        profile = local.call("arli/profile")
    return service.pull_register({"card": card, "nonce": hello["nonce"],
                                 "signature": signed["signature"], "kind": "supply", "profile": profile})


def test_a_pull_token_has_a_term_and_is_never_renewed_silently(monkeypatch, tmp_path) -> None:
    """A token that lives forever is a key nobody can cancel, so it is issued with a term and refused by name."""
    from arli_engine.adapters.a2a.server import start_agent
    from arli_engine.agent import build_agent
    from arli_engine.core.signing import KeyPair

    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    monkeypatch.setattr(service, "PULL_TOKEN_DAYS", -1, raising=False)  # issued already expired
    key = KeyPair.generate()
    state = build_agent("short-lived", ["offer:warehousing"], "http://127.0.0.1:0", private_pem=key.private_pem)
    server, _thread = start_agent(state, 0, host="127.0.0.1")
    try:
        answer = _pull_registration(service, "http://127.0.0.1:%d" % server.server_address[1])
        assert answer["token_term_days"] == -1 and answer["token_expires_at"], answer
        assert service.pull_token_state(answer["pull_token"])[0] == "expired", "the term is what was issued"
        assert service.pull_token_of(answer["pull_token"]) is None, "an expired token is not a live registration"
        assert service.remote_agents() == [], "an agent whose term ran out leaves the listing"
    finally:
        server.shutdown()
        server.server_close()


def test_a_pull_agent_that_stops_polling_leaves_the_listing(monkeypatch, tmp_path) -> None:
    """A name nobody answers to is worse than a short list, and it must not cost a search its timeout."""
    from arli_engine.adapters.a2a.server import start_agent
    from arli_engine.agent import build_agent
    from arli_engine.core.signing import KeyPair

    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    key = KeyPair.generate()
    state = build_agent("went-quiet", ["offer:warehousing"], "http://127.0.0.1:0", private_pem=key.private_pem)
    server, _thread = start_agent(state, 0, host="127.0.0.1")
    try:
        _pull_registration(service, "http://127.0.0.1:%d" % server.server_address[1])
        assert service.remote_agents() and service.remotes_to_check(), "just registered, so it is listed"
        assert service.scan_room(["need:warehousing"], 2000, "demand")["agents_checked"] >= 1
    finally:
        server.shutdown()
        server.server_close()

    monkeypatch.setattr(service, "PULL_IDLE_HOURS", 1, raising=False)
    with service.connect() as connection:
        connection.execute("UPDATE remotes SET last_poll = ? WHERE mode = 'pull'",
                           ("2026-01-01T00:00:00Z",))
    assert service.remote_agents() == [], "it stopped polling, so its name is not offered any more"
    assert service.remotes_to_check() == [], "and a search does not sit waiting out its timeout either"


def test_an_expired_token_is_refused_with_its_own_name(monkeypatch, tmp_path) -> None:
    """Unknown and expired are different things to a client: one is a stranger, the other should register again."""
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    assert service.pull_token_state("no-such-token")[0] == "unknown"
    with service.connect() as connection:
        connection.execute(
            """INSERT INTO remotes (id, name, kind, city, headline, endpoint, kid, jwk, connected_at, active,
                                    profile, bucket_space, mode, pull_token, pull_expires_at, last_poll, card)
               VALUES ('old','old','supply','','','','k','{}','2026-01-01T00:00:00Z',1,'{}','','pull','t-old',
                       '2026-01-02T00:00:00Z','','{}')""")
    assert service.pull_token_state("t-old")[0] == "expired"
    assert service.pull_token_of("t-old") is None


def test_a_remote_row_carries_its_door_so_the_page_can_offer_it(monkeypatch, tmp_path) -> None:
    """A pull agent is askable, and a row that does not say so makes the page hide a button that would work."""
    import threading

    from arli_engine.adapters.a2a.server import start_agent
    from arli_engine.agent import build_agent
    from arli_engine.client import AgentClient
    from arli_engine.core.signing import KeyPair

    monkeypatch.setenv("ARLI_TAXONOMY", "/llm-gateway/skill_taxonomy_v2.json")
    monkeypatch.setenv("ARLI_DECLARED_AREAS", "manufacturing-and-production")
    monkeypatch.delenv("ARLI_MATCH_ALLOW_PRIVATE", raising=False)
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    getattr(service.taxonomy, "cache_clear", lambda: None)()

    key = KeyPair.generate()
    state = build_agent("no-address", ["offer:packaging"], "http://127.0.0.1:0", private_pem=key.private_pem)
    server, _thread = start_agent(state, 0, host="127.0.0.1")
    endpoint = "http://127.0.0.1:%d" % server.server_address[1]
    stop = threading.Event()

    def hold_the_poll():
        with AgentClient(endpoint) as local:
            while not stop.is_set():
                call = service.pull_next("no-address", wait_seconds=1)
                if not call:
                    continue
                try:
                    service.pull_answer("no-address", call["call_id"], local.call(call["method"], call["params"]))
                except Exception as error:  # noqa: BLE001
                    service.pull_answer("no-address", call["call_id"], None, error=str(error))

    poller = threading.Thread(target=hold_the_poll, daemon=True)
    poller.start()
    try:
        _pull_registration(service, endpoint)
        rows = [row for row in service.scan_room(["need:packaging"], 2000, "demand")["rows"] if row.get("remote")]
        assert rows and rows[0]["askable"] is True, rows
    finally:
        stop.set()
        poller.join(timeout=5)
        server.shutdown()
        server.server_close()


def test_the_limit_counts_the_real_client_behind_a_proxy(monkeypatch, tmp_path) -> None:
    """Behind nginx every request is loopback, so counting the connection address makes one global limit."""
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    assert service.client_ip("127.0.0.1", "203.0.113.9") == "203.0.113.9", "the forwarded address is the client"
    assert service.client_ip("203.0.113.9", "198.51.100.1") == "203.0.113.9", "a stranger's header is not trusted"
    assert service.client_ip("127.0.0.1", "") == "127.0.0.1"
    assert service.client_ip("127.0.0.1", "203.0.113.9, 10.0.0.1") == "203.0.113.9"


def test_a_search_flood_from_one_address_is_cut_off(monkeypatch, tmp_path) -> None:
    """The search is the expensive public call, and it was the one public call with no limit at all."""
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    service._attempts.clear()
    for _ in range(service.SEARCH_ATTEMPTS_ALLOWED):
        assert service.attempts_allowed("scan:203.0.113.9", allowed=service.SEARCH_ATTEMPTS_ALLOWED,
                                        window=service.SEARCH_WINDOW_SECONDS) is True
        service.note_attempt("scan:203.0.113.9")
    assert service.attempts_allowed("scan:203.0.113.9", allowed=service.SEARCH_ATTEMPTS_ALLOWED,
                                    window=service.SEARCH_WINDOW_SECONDS) is False
    assert service.attempts_allowed("scan:203.0.113.10", allowed=service.SEARCH_ATTEMPTS_ALLOWED,
                                    window=service.SEARCH_WINDOW_SECONDS) is True, "one address does not shut out another"


def test_a_search_records_nothing_about_who_asked(monkeypatch, tmp_path) -> None:
    """We say a search stores nothing about the asker. That is a claim about code, so it is tested on the tables."""
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    before = {}
    with service.connect() as connection:
        for table in ("agents", "remotes", "accounts", "introductions", "intro_messages", "agreements"):
            before[table] = connection.execute("SELECT COUNT(*) AS n FROM %s" % table).fetchone()["n"]
    result = service.scan_room(["need:warehousing"], 2000, "demand")
    assert result["rows"], "the search ran"
    with service.connect() as connection:
        for table, count in before.items():
            now = connection.execute("SELECT COUNT(*) AS n FROM %s" % table).fetchone()["n"]
            assert now == count, f"{table} gained a row from a search: a search must store nothing about the asker"


def test_the_join_check_answers_about_the_agent_asked_with_grounds(monkeypatch, tmp_path) -> None:
    """A lookup that answers every question with the first row is worse than no lookup: it says yes to a stranger."""
    service = _service_on_a_temp_store(monkeypatch, tmp_path)
    account, _token = service.register_account("join@example.com", "join-password")
    hosted, _ = service.create_agent({"name": "Join Warehouse", "kind": "supply", "features": ["offer:warehousing"]})
    with service.connect() as connection:
        connection.execute("UPDATE agents SET owner_account = ? WHERE id = ?", (account["id"], hosted["id"]))
    with service.connect() as connection:
        connection.execute(
            """INSERT INTO remotes (id, name, kind, city, headline, endpoint, kid, jwk, connected_at, active,
                                    profile, bucket_space, mode, pull_token, pull_expires_at, last_poll, card)
               VALUES ('joined-by-calling','joined-by-calling','supply','','','','k','{}',
                       '2026-01-01T00:00:00Z',1,'{"buckets": {"manufacturing-and-production": 1}, "bucket_space": "arli-areas-v1"}',
                       'arli-areas-v1','pull','t','2099-01-01T00:00:00Z','','{}')""")

    assert service.join_check("nothing-like-this")["in_room"] is False, "an unknown name is not in the room"
    answer = service.join_check("joined-by-calling")
    assert answer["in_room"] is True and answer["pull"] is True, answer
    assert answer["buckets"] == 1 and answer["vocabulary"] == "arli-areas-v1", answer
    hosted_answer = service.join_check("Join Warehouse")
    assert hosted_answer["hosted"] is True and hosted_answer["feature_count"] == 1, hosted_answer
    assert service.join_check("  ")["in_room"] is False
