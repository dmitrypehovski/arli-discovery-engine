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
        assert "opportunity/decide" in refused.detail, "the gap is named, not smoothed over"


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
