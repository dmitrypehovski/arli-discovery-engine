"""The loop's decisions, kept out of the process so they can be tested without waiting a minute."""

import urllib.error
from types import SimpleNamespace

from arli_engine.client import AgentRefused
from arli_engine import pull


def _args():
    return SimpleNamespace(kind="supply", city="", headline="", once=False, room="https://room.example")


def test_a_refusal_from_our_own_rate_window_is_a_wait_not_a_crash() -> None:
    """The agent the loop registers signs one nonce a minute, so a repeat is refused by ourselves."""
    error = AgentRefused("rate_limited", "one signed nonce per address and purpose per 60s")
    assert pull.wait_for(error) == pull.RATE_WAIT_SECONDS, "waiting is the whole fix"
    assert pull.wait_for(RuntimeError("something else")) == pull.RETRY_SECONDS


def test_registration_classifies_wait_and_fatal_and_success(monkeypatch) -> None:
    class Client:
        def __init__(self, behaviour):
            self.behaviour = behaviour

        def call(self, method, params=None):
            if isinstance(self.behaviour, Exception):
                raise self.behaviour
            return self.behaviour

    monkeypatch.setattr(pull, "register", lambda room, local, kind, city, headline: {"registered": True,
                                                                                    "pull_token": "t-1"})
    assert pull.registration_attempt("room", Client({}), _args()) == ("registered", "t-1", 0)

    monkeypatch.setattr(pull, "register",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AgentRefused("rate_limited", "one signed nonce per address and purpose per 60s")))
    outcome, detail, wait = pull.registration_attempt("room", Client({}), _args())
    assert outcome == "wait" and wait == pull.RATE_WAIT_SECONDS, (outcome, detail, wait)

    monkeypatch.setattr(pull, "register", lambda *a, **k: {"registered": False, "error": "nope"})
    assert pull.registration_attempt("room", Client({}), _args())[0] == "fatal"

    def refuse_with_http(*a, **k):
        raise urllib.error.HTTPError("u", 400, "bad", {}, None)

    monkeypatch.setattr(pull, "register", refuse_with_http)
    outcome, detail, wait = pull.registration_attempt("room", Client({}), _args())
    assert outcome == "fatal" and "400" in detail, (outcome, detail)


def test_a_rate_limited_room_reply_is_waited_out(monkeypatch) -> None:
    """The room can refuse for a window of its own, and the answer says which window it is."""
    class Body:
        def read(self, n=None):
            return b'{"error": "rate_limited"}'

        def close(self):
            return None

    def refuse(*a, **k):
        raise urllib.error.HTTPError("u", 429, "slow down", {}, Body())

    monkeypatch.setattr(pull, "register", refuse)
    outcome, detail, wait = pull.registration_attempt("room", SimpleNamespace(), _args())
    assert outcome == "wait" and wait == pull.RATE_WAIT_SECONDS, (outcome, detail, wait)


def test_the_identity_window_is_a_deployment_decision(monkeypatch) -> None:
    """Sixty seconds is right for a real agent and useless for proving a wait, so it can be stated."""
    from arli_engine.adapters.a2a.server import IDENTITY_WINDOW_SECONDS, identity_window_seconds

    monkeypatch.delenv("ARLI_IDENTITY_WINDOW_SECONDS", raising=False)
    assert identity_window_seconds() == IDENTITY_WINDOW_SECONDS
    monkeypatch.setenv("ARLI_IDENTITY_WINDOW_SECONDS", "6")
    assert identity_window_seconds() == 6
    monkeypatch.setenv("ARLI_IDENTITY_WINDOW_SECONDS", "not-a-number")
    assert identity_window_seconds() == IDENTITY_WINDOW_SECONDS, "a bad value falls back, it does not explode"


def test_a_dropped_poll_is_waited_out_not_fatal(monkeypatch) -> None:
    """A room restart drops the open poll. RemoteDisconnected is not a URLError, and catching only that killed
    the loop on a normal operational event, after which the room could not reach the agent at all."""
    import http.client

    def dropped(*a, **k):
        raise http.client.RemoteDisconnected("Remote end closed connection without response")

    monkeypatch.setattr(pull, "register", dropped)
    outcome, detail, wait = pull.registration_attempt("room", SimpleNamespace(), _args())
    assert outcome == "wait" and wait == pull.RETRY_SECONDS, (outcome, detail)
    assert "RemoteDisconnected" in detail
