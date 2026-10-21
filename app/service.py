"""ARLI Match as a real service: agents are stored, not raised per request.

Why this file exists. The demonstration raises two throwaway agents for one exchange. A service has
to do the opposite: an agent you register has to still be there tomorrow, with its own feature list
kept private, and a scan has to run against the agents that are actually in the room.

Storage is SQLite next to the service. Feature lists live in that file and are never returned by any
public endpoint: the room publishes identity, claim level and the size of the list, nothing else.

Run: .venv/bin/python app/service.py --port 8141
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sqlite3
import ssl
import threading
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arli_engine.client.agent import AgentClient  # noqa: E402
from arli_engine.core.intake import Taxonomy, read_request  # noqa: E402
from arli_engine.core.surface import CALL_REJECTIONS as CALL_REFUSAL_CODES  # noqa: E402

from app.network import (  # noqa: E402
    ConnectRefused, allow_private_addresses, normalize_endpoint, run_remote_exchange,
    verify_endpoint, verify_signed_nonce,
)
from app.server import DEFAULT_FLOOR, DEFAULT_MINE, ROSTER, Handler as BaseHandler, public_view  # noqa: E402
from demo.exchange import run_exchange  # noqa: E402

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ARLI_MATCH_DB", "/var/lib/arli-match/match.sqlite3"))
MAX_FEATURES = 64


def connect() -> sqlite3.Connection:
    """Open the store and make sure the schema is there.

    The schema is created here rather than only at startup: a store can be deleted or moved under a
    running service, and a request must not be the thing that discovers it.
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agents (
               id TEXT PRIMARY KEY,
               name TEXT NOT NULL,
               kind TEXT NOT NULL,
               city TEXT NOT NULL DEFAULT '',
               headline TEXT NOT NULL DEFAULT '',
               claim_level TEXT NOT NULL DEFAULT 'declared',
               will_not_publish TEXT NOT NULL DEFAULT '',
               features TEXT NOT NULL,
               owner_token TEXT NOT NULL,
               created_at TEXT NOT NULL,
               active INTEGER NOT NULL DEFAULT 1
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS remotes (
               id TEXT PRIMARY KEY,
               name TEXT NOT NULL,
               kind TEXT NOT NULL,
               city TEXT NOT NULL DEFAULT '',
               headline TEXT NOT NULL DEFAULT '',
               endpoint TEXT NOT NULL,
               kid TEXT NOT NULL DEFAULT '',
               jwk TEXT NOT NULL DEFAULT '{}',
               connected_at TEXT NOT NULL,
               active INTEGER NOT NULL DEFAULT 1,
               profile TEXT NOT NULL DEFAULT '{}',
               bucket_space TEXT NOT NULL DEFAULT '',
               mode TEXT NOT NULL DEFAULT 'address',
               pull_token TEXT NOT NULL DEFAULT '',
               pull_expires_at TEXT NOT NULL DEFAULT '',
               last_poll TEXT NOT NULL DEFAULT '',
               card TEXT NOT NULL DEFAULT '{}'
           )"""
    )
    # Added after the first rows existed: a remote agent that stops answering has to leave the room,
    # and the reason has to be visible rather than silent.
    connection.execute(
        """CREATE TABLE IF NOT EXISTS accounts (
               id TEXT PRIMARY KEY,
               email TEXT NOT NULL UNIQUE,
               password_hash TEXT NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agreements (
               id TEXT PRIMARY KEY,
               introduction_id TEXT NOT NULL,
               version INTEGER NOT NULL DEFAULT 1,
               proposed_by TEXT NOT NULL,
               proposed_by_email TEXT NOT NULL,
               accepted_by TEXT NOT NULL DEFAULT '',
               accepted_by_email TEXT NOT NULL DEFAULT '',
               terms TEXT NOT NULL,
               body TEXT NOT NULL,
               state TEXT NOT NULL,
               created_at TEXT NOT NULL,
               accepted_at TEXT NOT NULL DEFAULT '',
               signature_proposer TEXT NOT NULL,
               signature_acceptor TEXT NOT NULL DEFAULT '',
               evidence_ids TEXT NOT NULL DEFAULT '[]'
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS evidence_records (
               id TEXT PRIMARY KEY,
               subject TEXT NOT NULL,
               envelope TEXT NOT NULL,
               created_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS agreement_events (
               id TEXT PRIMARY KEY,
               agreement_id TEXT NOT NULL,
               kind TEXT NOT NULL,
               actor TEXT NOT NULL,
               note TEXT NOT NULL DEFAULT '',
               created_at TEXT NOT NULL,
               evidence_id TEXT NOT NULL
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS sessions (
               token TEXT PRIMARY KEY,
               account_id TEXT NOT NULL,
               created_at TEXT NOT NULL,
               expires_at TEXT NOT NULL
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS introductions (
               id TEXT PRIMARY KEY,
               from_account TEXT NOT NULL,
               from_agent TEXT NOT NULL,
               to_agent TEXT NOT NULL,
               to_account TEXT NOT NULL DEFAULT '',
               note TEXT NOT NULL DEFAULT '',
               state TEXT NOT NULL,
               created_at TEXT NOT NULL,
               decided_at TEXT NOT NULL DEFAULT '',
               opened_at TEXT NOT NULL DEFAULT '',
               negotiation_id TEXT NOT NULL DEFAULT '',
               remote_endpoint TEXT NOT NULL DEFAULT '',
               evidence_ids TEXT NOT NULL DEFAULT '[]'
           )"""
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS intro_messages (
               id TEXT PRIMARY KEY,
               introduction_id TEXT NOT NULL,
               author TEXT NOT NULL,
               body TEXT NOT NULL,
               created_at TEXT NOT NULL,
               evidence_id TEXT NOT NULL
           )"""
    )
    # A connected agent publishes a coarse profile, so a search can sift instead of asking everybody.
    for column, definition in (("failures", "INTEGER NOT NULL DEFAULT 0"),
                               ("last_checked", "TEXT NOT NULL DEFAULT ''"),
                               ("unreachable_reason", "TEXT NOT NULL DEFAULT ''"),
                               ("profile", "TEXT NOT NULL DEFAULT '{}'"),
                               ("bucket_space", "TEXT NOT NULL DEFAULT ''"),
                               ("mode", "TEXT NOT NULL DEFAULT 'address'"),
                               ("pull_token", "TEXT NOT NULL DEFAULT ''"),
                               ("pull_expires_at", "TEXT NOT NULL DEFAULT ''"),
                               ("last_poll", "TEXT NOT NULL DEFAULT ''"),
                               ("card", "TEXT NOT NULL DEFAULT '{}'")):
        try:
            connection.execute(f"ALTER TABLE remotes ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass
    # Which account made an agent, so a signed-in person can find their own again.
    try:
        connection.execute("ALTER TABLE agents ADD COLUMN owner_account TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # Introductions to agents we do not host carry the negotiation that happened on the other side.
    for column, definition in (("negotiation_id", "TEXT NOT NULL DEFAULT ''"),
                               ("remote_endpoint", "TEXT NOT NULL DEFAULT ''")):
        try:
            connection.execute(f"ALTER TABLE introductions ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass
    # An account has a signing key of its own, so what it agreed to can be checked by a stranger later.
    for column, definition in (("kid", "TEXT NOT NULL DEFAULT ''"),
                               ("public_jwk", "TEXT NOT NULL DEFAULT '{}'"),
                               ("private_pem", "TEXT NOT NULL DEFAULT ''")):
        try:
            connection.execute(f"ALTER TABLE accounts ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass
    return connection


def init() -> None:
    with connect():
        pass


def connect_agent(payload: dict, room: str = "") -> dict:
    """Connect somebody else's agent: verify its card, then require the address to sign our nonce.

    What this proves and what it does not. The card's signature proves the key published this agent,
    and the signature over a nonce this service chose proves the same key answers at that address
    right now. It does not prove the address is run by the key's owner: a proxy that forwards the
    signing passes too, and it then holds a live path to the agent, which is the trust a relay has.
    Say so instead of implying more.
    """
    endpoint = normalize_endpoint(str(payload.get("endpoint") or ""),
                                  allow_private=allow_private_addresses())
    kind = str(payload.get("kind") or "").strip()
    if kind not in ("demand", "supply", "person"):
        raise ConnectRefused("kind_required", "declare the agent's type: demand, supply or person")

    verified = verify_endpoint(endpoint)
    # One call does two jobs: it proves the extension answers, and it proves the address holds the
    # key the card names, because the nonce is ours and the bytes are signed exactly as sent.
    nonce = secrets.token_urlsafe(24)
    try:
        Client = __import__("arli_engine.client", fromlist=["AgentClient"]).AgentClient
        with Client(endpoint) as client:
            # The purpose names the room, because the agent it asks limits one signature per address and
            # purpose per minute. A fixed word would make the second room an agent belongs to look like a
            # repeat of the first and be refused, which is the wrong refusal for the right reason.
            answer = client.call("arli/identity/sign",
                                 {"nonce": nonce, "purpose": "connect:" + (room.strip().lower() or "room")})
    except Exception as error:  # noqa: BLE001
        # The agent's own refusal is more useful than a shrug: a rate limit, a short nonce and a
        # missing purpose all mean different things to the person holding the address.
        code = getattr(error, "code", None)
        if code in CALL_REFUSAL_CODES:
            raise ConnectRefused(code, f"{endpoint} refused: {error}") from error
        raise ConnectRefused(
            "agent_not_answering",
            f"{endpoint} served a card but did not answer the identity method: "
            f"{type(error).__name__}",
        ) from error
    if not isinstance(answer, dict) or not answer.get("signature"):
        raise ConnectRefused("agent_not_answering", f"{endpoint} answered without a signature")
    if not verify_signed_nonce(verified["jwk"], nonce, str(answer["signature"])):
        raise ConnectRefused(
            "address_key_mismatch",
            f"{endpoint} answered, but its signature over our nonce does not verify with the key "
            f"from its card: the address is not serving that key",
        )

    agent_id = verified["name"] or endpoint
    # One call, once, at the door: what does this agent look like coarsely? A room of thousands cannot run
    # an exchange with everybody on every search, so the profile is what a search sifts with. An agent that
    # does not answer it is not refused: it is marked unsifted and every search still asks it, which is the
    # price of not being able to place it, and it is said in the answer rather than hidden.
    buckets: dict = {}
    space = ""
    profile_answer: dict = {}
    try:
        with AgentClient(endpoint) as client:
            answer = client.call("arli/profile")
        if isinstance(answer, dict) and isinstance(answer.get("buckets"), dict):
            buckets = {str(slug): int(count) for slug, count in answer["buckets"].items()}
            space = str(answer.get("bucket_space") or "")
            profile_answer = answer
    except Exception:  # noqa: BLE001
        buckets, space, profile_answer = {}, "", {}

    with connect() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO remotes
               (id, name, kind, city, headline, endpoint, kid, jwk, connected_at, active,
                profile, bucket_space)
               VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
            (agent_id, verified["name"], kind, str(payload.get("city") or "").strip(),
             str(payload.get("headline") or "").strip(), endpoint, verified["kid"],
             json.dumps(verified["jwk"]), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             json.dumps(profile_answer), space),
        )
        # Connecting again is the way back: the count resets and the agent is in the room once more.
        connection.execute("UPDATE remotes SET failures = 0, unreachable_reason = '', active = 1 WHERE id = ?",
                           (agent_id,))
    return {
        "connected": True,
        "id": agent_id,
        "name": verified["name"],
        "kind": kind,
        "endpoint": endpoint,
        "checks": {"card_verified": True, "extension_answers": True, "address_signed_our_nonce": True},
        "profile": {"buckets": len(buckets), "bucket_space": space or "not published",
                    "sifted": bool(space) and bucket_space_name() == space,
                    "note": "A search sifts by this profile when both sides count in the same space. "
                            "Otherwise the agent is asked on every search, which costs an exchange."},
        "note": "Connected from its own address. The card verified, the identity method answered, and "
                "the address signed a nonce this service chose, so the key the card names is reachable "
                "there. A proxy that forwards that signing passes too: it then holds a live path to "
                "the agent, which is the trust a relay has.",
    }


# A room cannot call an agent that sits behind a router it cannot see through. Such an agent calls the room
# instead: it says hello, signs the room's nonce, and then holds a poll open. A call for it is queued here and
# answered on the agent's next poll, so the exchange runs without any inbound path to the agent at all.
PULL_WAIT_SECONDS = 45
PULL_POLL_SECONDS = 20
# A token issued at registration is issued with a term rather than living forever, and it is not renewed
# silently: an agent that comes back gets a new one, and that is visible in its own log. An agent that stops
# polling leaves the listing, because a listing that keeps a name nobody answers to is worse than a short one.
PULL_TOKEN_DAYS = int(os.environ.get("ARLI_MATCH_PULL_DAYS") or 30)
PULL_IDLE_HOURS = int(os.environ.get("ARLI_MATCH_PULL_IDLE_HOURS") or 72)


def _after(**delta) -> str:
    return (datetime.now(timezone.utc) + timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_past(stamp: str) -> bool:
    if not stamp:
        return False
    try:
        when = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return True
    return when <= datetime.now(timezone.utc)


def _pull_is_live(row: dict) -> bool:
    """Whether a pull agent is still somebody the room can reach: unexpired token and a recent poll."""
    if _is_past(str(row.get("pull_expires_at") or "")):
        return False
    last = str(row.get("last_poll") or "")
    if not last:
        return True  # just registered, has not polled yet
    try:
        when = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - when) <= timedelta(hours=PULL_IDLE_HOURS)
_pull_lock = threading.Lock()
_pull_queue: dict[str, list[str]] = {}
_pull_calls: dict[str, dict] = {}


class PullTimeout(RuntimeError):
    """The agent did not answer its poll in time. Named, because it is not a refusal."""


def _pull_nonce_store() -> dict:
    return _PULL_NONCES


_PULL_NONCES: dict[str, tuple[str, float]] = {}


def _card_subject(card: dict) -> tuple[str, dict]:
    """Which key and identity this card stands for, read the same way in both halves of registration.

    A card keeps its key in a signature block rather than at the top level, and reading it in two places with
    different fallbacks is how a nonce gets filed under one name and looked up under another. One reader, one
    answer.
    """
    from app.network import card_key

    jwk, kid = card_key(card)
    return str(kid or card.get("name") or ""), jwk


def pull_hello(card: dict) -> dict:
    """First half of registering an agent that calls us: prove you hold the card's key, here is our nonce."""
    from app.network import verify_card_self

    verified, reasons = verify_card_self(card)
    if not verified:
        raise ConnectRefused("card_not_verified", "; ".join(reasons) or "signature does not verify")
    subject, _jwk = _card_subject(card)
    nonce = secrets.token_hex(16)
    _PULL_NONCES[subject] = (nonce, time.time())
    return {"nonce": nonce, "purpose": "pull:register",
            "answer": "Sign this nonce with the key your card names and post it back with the answer to "
                      "arli/profile. Nothing else is needed, and no address."}


def pull_register(payload: dict) -> dict:
    """Second half: the signature over our nonce, the profile, and a token for the poll."""
    from app.network import verify_card_self, verify_signed_nonce

    card = payload.get("card") or {}
    verified, reasons = verify_card_self(card)
    if not verified:
        raise ConnectRefused("card_not_verified", "; ".join(reasons) or "signature does not verify")
    subject, jwk = _card_subject(card)
    nonce, issued = _PULL_NONCES.get(subject, ("", 0.0))
    given = str(payload.get("nonce") or "")
    if not nonce or given != nonce:
        raise ConnectRefused("nonce_not_ours", "register with the nonce this room issued to this card")
    if time.time() - issued > 300:
        raise ConnectRefused("nonce_expired", "the nonce this room issued is more than five minutes old")
    if not verify_signed_nonce(jwk, given, str(payload.get("signature") or "")):
        raise ConnectRefused("address_key_mismatch",
                             "the signature over our nonce does not verify with the key the card names")
    profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
    agent_id = str(card.get("name") or subject or "pull-agent")
    token = secrets.token_urlsafe(24)
    with connect() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO remotes
               (id, name, kind, city, headline, endpoint, kid, jwk, connected_at, active,
                profile, bucket_space, mode, pull_token, pull_expires_at, last_poll, card)
               VALUES (?,?,?,?,?,'',?,?,?,1,?,?,'pull',?,?,'',?)""",
            (agent_id, agent_id, str(payload.get("kind") or "supply").strip() or "supply",
             str(payload.get("city") or "").strip(), str(payload.get("headline") or "").strip(), subject,
             json.dumps(jwk), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             json.dumps(profile), str(profile.get("bucket_space") or ""), token,
             _after(days=PULL_TOKEN_DAYS), json.dumps(card)))
    return {"registered": True, "id": agent_id, "pull_token": token,
            "token_expires_at": _after(days=PULL_TOKEN_DAYS),
            "token_term_days": PULL_TOKEN_DAYS,
            "checks": {"card_verified": True, "our_nonce_signed_by_the_card_key": True,
                       "reachability": "outbound only: this agent calls the room, and the room cannot call it"},
            "note": "There is no address to test here, and that is the point. The key was proved by what the "
                    "agent signed, and every exchange with it now travels over the poll it holds open. The token "
                    "is issued with a term and is not renewed silently: when it runs out, register again."}


def pull_token_state(token: str) -> tuple[str, dict | None]:
    """Ok, expired or unknown, and never a silent renewal: an expired token is refused by name."""
    if not token:
        return "unknown", None
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM remotes WHERE pull_token = ? AND mode = 'pull' AND active = 1", (token,)).fetchone()
    if row is None:
        return "unknown", None
    agent = dict(row)
    if _is_past(str(agent.get("pull_expires_at") or "")):
        return "expired", agent
    return "ok", agent


def pull_token_of(token: str) -> dict | None:
    """The agent this token belongs to, or None when it is unknown or its term has run out."""
    state, agent = pull_token_state(token)
    return agent if state == "ok" else None


def pull_next(agent_id: str, wait_seconds: int = PULL_POLL_SECONDS) -> dict | None:
    """One pending call for this agent, or None if none arrived before the poll ends."""
    with connect() as connection:
        connection.execute("UPDATE remotes SET last_poll = ? WHERE id = ? AND mode = 'pull'",
                           (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), agent_id))
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        with _pull_lock:
            queued = _pull_queue.get(agent_id) or []
            if queued:
                call_id = queued.pop(0)
                call = _pull_calls.get(call_id)
                if call is not None and not call["answered"]:
                    return {"call_id": call_id, "method": call["method"], "params": call["params"]}
        time.sleep(0.25)
    return None


def pull_answer(agent_id: str, call_id: str, result: dict | None, error: str = "") -> bool:
    with _pull_lock:
        call = _pull_calls.get(call_id)
        if call is None or call["agent_id"] != agent_id or call["answered"]:
            return False
        call["result"] = result
        call["error"] = error
        call["answered"] = True
        call["event"].set()
    return True


JOIN_CHECKS_ALLOWED = int(os.environ.get("ARLI_MATCH_JOIN_CHECKS") or 60)


def room_listing() -> dict:
    """Who is in the room right now, told the way the room knows them.

    What is published about each agent: the name it registered under, its kind, its city and its headline, which
    are already public in a search result, plus how it is reached and how many areas it publishes. What is NOT
    published: the address of an agent at its own address. A listing of addresses is a target list, and the room
    already refuses to hand one out in a search; a directory is not going to be the hole that leaks it. Examples
    are kept in their own group, so nobody reads them as a market.
    """
    hosted, connected, examples = [], [], []
    for agent in stored_agents():
        hosted.append({"name": agent["name"], "kind": agent["kind"], "city": agent["city"],
                       "headline": agent["headline"], "reached": "hosted here",
                       "items": agent.get("feature_count"), "areas": None})
    for remote in remotes_to_check():
        if not remote.get("active"):
            continue  # it left the room; its name is not offered anywhere else either
        pull = (remote.get("mode") or "address") == "pull"
        if pull and not _pull_is_live(remote):
            continue
        try:
            profile = json.loads(remote.get("profile") or "{}")
        except json.JSONDecodeError:
            profile = {}
        connected.append({"name": remote["name"], "kind": remote["kind"], "city": remote["city"],
                          "headline": remote["headline"],
                          "reached": ("by calling the room and holding it open" if pull else "at its own address"),
                          "pull": pull, "items": None,
                          "areas": len(profile.get("buckets") or {}) or None,
                          "vocabulary": str(profile.get("bucket_space") or "")})
    if shows_examples():
        for agent in ROSTER:
            view = public_view(agent)
            examples.append({"name": view["name"], "kind": view["kind"], "city": view["city"],
                             "headline": view["headline"], "reached": "example: nobody to ask"})
    return {"mode": room_mode(), "counts": {"hosted": len(hosted), "connected": len(connected),
                                            "examples": len(examples)},
            "hosted": hosted, "connected": connected, "examples": examples,
            "note": "Addresses of agents at their own address are not listed here and are not listed in a search "
                    "either. An example agent is not a market."}


def join_check(what: str) -> dict:
    """Is this agent in the room, and how does the room reach it?

    What this does: looks up what is already registered here — a hosted agent, an agent connected at its own
    address, or one that registers by calling. What it deliberately does not do: dial the address it was given.
    A public endpoint that knocks on arbitrary addresses is a scanner handed to strangers, and the answer to "is my
    agent reachable" is already here for anything that registered.
    """
    wanted = what.strip().lower()
    if not wanted:
        return {"in_room": False, "detail": "nothing was given"}
    bare = wanted.rstrip("/")

    for agent in stored_agents(include_features=True):
        if wanted in (str(agent["id"]).lower(), str(agent["name"]).lower()):
            buckets = seeker_buckets(agent.get("features") or [])
            return {"in_room": True, "name": agent["name"], "id": agent["id"],
                    "how": "registered here, so this room holds its list because its owner made it from words",
                    "reachable": "any search can find it without an account",
                    "feature_count": agent["feature_count"], "buckets": len(buckets),
                    "vocabulary": VOCABULARY_ID, "pull": False, "hosted": True}

    for remote in remote_agents(include_endpoint=True):
        endpoint = str(remote.get("endpoint") or "").lower().rstrip("/")
        # The value searched for is not in the tuple compared against, or the comparison is true for everything:
        # that mistake was made here and answered every question with the first agent in the room.
        if wanted in (str(remote["id"]).lower(), str(remote["name"]).lower()) or bare == endpoint:
            profile = {}
            row = next((item for item in remotes_to_check() if item["id"] == remote["id"]), {})
            try:
                profile = json.loads(row.get("profile") or "{}")
            except json.JSONDecodeError:
                profile = {}
            pull = bool(remote.get("pull"))
            return {"in_room": True, "name": remote["name"], "id": remote["id"],
                    "how": ("an agent that has no address: it calls the room and answers on its own poll" if pull
                            else "an agent at its own address"),
                    "reachable": ("it answers on its next poll, in seconds" if pull
                                  else "the room reaches it at that address"),
                    "feature_count": None, "buckets": len(profile.get("buckets") or {}),
                    "vocabulary": str(profile.get("bucket_space") or ""), "pull": pull, "hosted": False,
                    "endpoint": remote.get("endpoint") if not pull else ""}

    return {"in_room": False,
            "detail": "no agent with that name or address is registered here. Either it was never connected, or it "
                      "left the listing because its term ran out or it stopped answering."}


def peer_client_for(remote: dict):
    """How to reach this agent: over its address, or over the poll it holds open."""
    if remote.get("mode") != "pull":
        return None
    try:
        card = json.loads(remote.get("card") or "{}")
    except json.JSONDecodeError:
        card = {}
    return PullClient(remote["id"], card=card if isinstance(card, dict) else {})


def pull_call(agent_id: str, method: str, params: dict, timeout: int = PULL_WAIT_SECONDS) -> dict:
    """Ask an agent that holds a poll open, and wait for its answer."""
    call_id = secrets.token_hex(10)
    event = threading.Event()
    with _pull_lock:
        _pull_calls[call_id] = {"agent_id": agent_id, "method": method, "params": params,
                                "answered": False, "result": None, "error": "", "event": event,
                                "asked_at": time.time()}
        _pull_queue.setdefault(agent_id, []).append(call_id)
    if not event.wait(timeout):
        with _pull_lock:
            _pull_calls.pop(call_id, None)
            queued = _pull_queue.get(agent_id) or []
            if call_id in queued:
                queued.remove(call_id)
        raise PullTimeout(f"{agent_id} did not answer within {timeout}s of the poll")
    with _pull_lock:
        call = _pull_calls.pop(call_id, {}) or {}
    if call.get("error"):
        raise AgentRefused(str(call["error"]))
    return call.get("result") or {}


class PullClient:
    """The same shape as AgentClient, over the poll instead of over an address.

    The exchange code does not know the difference, which is the point: reachability became a transport detail
    rather than a property an agent has to own.
    """

    def __init__(self, agent_id: str, card: dict | None = None, timeout: int = PULL_WAIT_SECONDS) -> None:
        self.agent_id = agent_id
        self._card = card or {}
        self.timeout = timeout

    def get_card(self) -> dict:
        if self._card:
            return self._card
        raise PullTimeout(f"{self.agent_id} has no card on record for this room")

    def call(self, method: str, params: dict | None = None) -> dict:
        return pull_call(self.agent_id, method, params or {}, timeout=self.timeout)

    def close(self) -> None:
        return None

    def __enter__(self) -> "PullClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def remote_agents(include_endpoint: bool = False) -> list[dict]:
    with connect() as connection:
        rows = connection.execute("SELECT * FROM remotes WHERE active = 1 ORDER BY connected_at").fetchall()
    agents = []
    for row in rows:
        if (row["mode"] if "mode" in row.keys() else "address") == "pull" and not _pull_is_live(dict(row)):
            continue  # its term ran out, or it stopped polling: it is not reachable and must not be listed
        agent = {"id": row["id"], "name": row["name"], "kind": row["kind"], "city": row["city"],
                 "headline": row["headline"], "claim_level": "declared",
                 "will_not_publish": "whatever its owner keeps at home",
                 "feature_count": None, "remote": True, "connected_at": row["connected_at"],
                 "pull": (row["mode"] if "mode" in row.keys() else "address") == "pull"}
        if include_endpoint:
            agent["endpoint"] = row["endpoint"]
            agent["kid"] = row["kid"]
        agents.append(agent)
    return agents


def remotes_to_check() -> list[dict]:
    """Every connected agent, including the ones that left the room.

    A search retries a withdrawn agent quietly. Without that, a moment of network trouble would be
    permanent: the agent would have to be connected again by hand to come back.
    """
    with connect() as connection:
        rows = connection.execute("SELECT * FROM remotes ORDER BY connected_at").fetchall()
    keep = []
    for row in rows:
        # A stale pull agent must not be asked either, or every search would sit waiting out its timeout.
        if (row["mode"] if "mode" in row.keys() else "address") == "pull" and not _pull_is_live(dict(row)):
            continue
        keep.append(row)
    rows = keep
    return [{"id": row["id"], "name": row["name"], "kind": row["kind"], "city": row["city"],
             "headline": row["headline"], "endpoint": row["endpoint"], "active": row["active"],
             "failures": row["failures"], "profile": row["profile"] if "profile" in row.keys() else "{}",
             "bucket_space": row["bucket_space"] if "bucket_space" in row.keys() else "",
             "mode": row["mode"] if "mode" in row.keys() else "address",
             "pull_token": row["pull_token"] if "pull_token" in row.keys() else "",
             "card": row["card"] if "card" in row.keys() else "{}"} for row in rows]


def stored_agents(include_features: bool = False) -> list[dict]:
    with connect() as connection:
        rows = connection.execute("SELECT * FROM agents WHERE active = 1 ORDER BY created_at").fetchall()
    agents = []
    for row in rows:
        agent = {
            "id": row["id"], "name": row["name"], "kind": row["kind"], "city": row["city"],
            "headline": row["headline"], "claim_level": row["claim_level"],
            "will_not_publish": row["will_not_publish"], "stored": True,
            "feature_count": len(json.loads(row["features"])),
        }
        if include_features:
            agent["features"] = json.loads(row["features"])
        agents.append(agent)
    return agents


ROOM_MODE_DEMO = "demo"
ROOM_MODE_REAL = "real"
ROOM_MODE_ENV = "ARLI_MATCH_ROOM_MODE"


def room_mode() -> str:
    """Which room this is.

    A demonstration room is filled with example agents, because an empty room explains nothing. A real
    room must not be: a participant who scans it and finds eight fictional companies has been told
    something untrue, and their match count is fiction too. The default stays `demo` so nothing changes
    until the first real room is switched on deliberately.
    """
    return ROOM_MODE_REAL if os.environ.get(ROOM_MODE_ENV, "").strip().lower() == ROOM_MODE_REAL else ROOM_MODE_DEMO


def shows_examples() -> bool:
    return room_mode() == ROOM_MODE_DEMO


def room_view() -> list[dict]:
    """Everyone in the room: starters, agents registered here, and agents connected from elsewhere.

    Never a feature list, and never a remote agent's address: an address is an invitation to probe.
    """
    samples = ([{**public_view(agent), "stored": False, "sample": True} for agent in ROSTER]
               if shows_examples() else [])
    stored = [{key: value for key, value in agent.items() if key != "features"}
              for agent in stored_agents(include_features=True)]
    return samples + stored + remote_agents(include_endpoint=False)


def create_agent(payload: dict) -> tuple[dict, str]:
    """An agent from a list, or from words it reads for itself.

    Both doors lead to the same place. A person who can write their list writes it; a person who cannot writes
    a sentence, and the same reader the intake uses names what it can and refuses to invent the rest. That is
    the difference between a network only programmers can join and one anybody can.
    """
    name = str(payload.get("name") or "").strip()
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"a name of {MAX_NAME_CHARS} characters at most, not {len(name)}")
    kind = str(payload.get("kind") or "").strip()
    features = [str(item).strip() for item in (payload.get("features") or []) if str(item).strip()]
    description = str(payload.get("description") or "").strip()
    read: dict | None = None

    if not features and description:
        read = read_request(description, taxonomy=taxonomy(), kind=kind or None)
        if not read["features"]:
            raise ValueError(
                "nothing in that description could be placed, so the agent would hold an empty list and "
                "match nobody. " + (read["questions"][0] if read["questions"] else "")
            )
        features = list(read["features"])
        kind = kind or read["kind"] or "demand"

    kind = kind or "demand"
    if not name:
        raise ValueError("name is required")
    if kind not in ("demand", "supply", "person"):
        raise ValueError(f"kind must be demand, supply or person, not {kind}")
    if not features:
        raise ValueError("at least one feature is required: an empty list matches nothing")
    if len(features) > MAX_FEATURES:
        raise ValueError(f"{len(features)} features is above the cap of {MAX_FEATURES} for one exchange")

    agent_id = str(payload.get("id") or "").strip() or f"a-{secrets.token_hex(5)}"
    token = secrets.token_urlsafe(24)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as connection:
        connection.execute(
            "INSERT INTO agents (id, name, kind, city, headline, claim_level, will_not_publish,"
            " features, owner_token, created_at, active) VALUES (?,?,?,?,?,?,?,?,?,?,1)",
            (
                agent_id, name, kind, str(payload.get("city") or "").strip(),
                str(payload.get("headline") or "").strip() or description[:140],
                str(payload.get("claim_level") or "declared").strip(),
                str(payload.get("will_not_publish") or "").strip(),
                json.dumps(features), token, created,
            ),
        )
    created_agent = {"id": agent_id, "name": name, "kind": kind, "feature_count": len(features),
                     "created_at": created}
    if read is not None:
        # What it understood itself to be, told to the person rather than kept: the same reading the intake
        # shows, so an agent made from words can be argued with.
        created_agent["read"] = read
    return created_agent, token


def owned_agent(agent_id: str, token: str) -> dict | None:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM agents WHERE id = ? AND owner_token = ?", (agent_id, token)
        ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "name": row["name"], "kind": row["kind"], "city": row["city"],
            "headline": row["headline"], "claim_level": row["claim_level"],
            "will_not_publish": row["will_not_publish"], "features": json.loads(row["features"]),
            "created_at": row["created_at"]}


# A connected agent that stops answering must not sit in the room for ever looking available. It
# leaves after two failed attempts and comes back the moment a scan reaches it or someone connects it
# again. Two, not one: a tunnel hiccup is not an agent that has gone away.
# A name is for reading in a room, not for carrying a payload.
MAX_NAME_CHARS = 80

REMOTE_FAILURES_BEFORE_WITHDRAWAL = 2


def note_remote_failure(agent_id: str, reason: str) -> bool:
    """Count a failed attempt. Returns True when this one takes the agent out of the room."""
    with connect() as connection:
        connection.execute(
            """UPDATE remotes
               SET failures = failures + 1,
                   last_checked = ?,
                   unreachable_reason = ?,
                   active = CASE WHEN failures + 1 >= ? THEN 0 ELSE active END
               WHERE id = ?""",
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), reason,
             REMOTE_FAILURES_BEFORE_WITHDRAWAL, agent_id),
        )
        row = connection.execute("SELECT failures, active FROM remotes WHERE id = ?",
                                 (agent_id,)).fetchone()
    return bool(row) and row["failures"] >= REMOTE_FAILURES_BEFORE_WITHDRAWAL and row["active"] == 0


def note_remote_success(agent_id: str) -> None:
    """An answer clears the count: the agent is here, and the room should say so."""
    with connect() as connection:
        connection.execute(
            """UPDATE remotes SET failures = 0, unreachable_reason = '', active = 1,
               last_checked = ? WHERE id = ?""",
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), agent_id),
        )


def features_from_payload(payload: dict) -> list[str]:
    """The caller's own list, with the two cases kept apart.

    Absent means "no list given, use the example". An empty list means an empty list, and the caller is
    told it matches nothing. One `or` used to collapse the two, so a caller who sent nothing received a
    full scan of the example features and no sign that the answer was not about their list.
    """
    raw = payload.get("my_features")
    values = DEFAULT_MINE if raw is None else raw
    return [str(item).strip() for item in values if str(item).strip()]


def looks_like_own_agent(result: dict, my_features: list[str]) -> bool:
    """A network agent matching every submitted feature is either your own or shares your whole list.

    The service is given a list, not an identity, so it cannot exclude anything. Saying what it sees is
    the honest move: a perfect overlap with a remote agent is worth a second look before acting on it.
    """
    return bool(my_features) and result.get("overlap_basis_points") == 10000


def scan_room(my_features: list[str], floor: int, my_kind: str) -> dict:
    """One exchange per agent in the room, samples and registered agents alike."""
    entries = []
    for agent in (ROSTER if shows_examples() else []):
        entries.append({"id": agent["id"], "name": agent["name"], "kind": agent["kind"],
                        "city": agent["city"], "headline": agent["headline"],
                        "claim_level": agent["claim_level"],
                        "will_not_publish": agent["will_not_publish"],
                        "feature_count": len(agent["features"]), "sample": True,
                        "features": list(agent["features"])})
    for agent in stored_agents(include_features=True):
        entries.append({**agent, "sample": False})

    # Who holds an agent is not a public fact, so the room carries the capability rather than the owner: an
    # agent is askable when there is a door for a request, an account here or an address to reach it at. A
    # registered agent whose maker never claimed it has neither, and a button that can only fail is worse than
    # no button.
    with connect() as connection:
        held = {row["id"] for row in connection.execute(
            "SELECT id FROM agents WHERE active = 1 AND owner_account <> ''")}

    rows = []
    for entry in entries:
        askable = bool(entry.get("sample") is False and (entry["id"] in held or entry.get("remote")))
        try:
            result = run_exchange(my_features, entry["features"], floor, seeker_name="you",
                                  holder_name=entry["id"], seeker_kind=my_kind,
                                  holder_kind=entry["kind"])
        except Exception as error:  # noqa: BLE001
            rows.append({"id": entry["id"], "name": entry["name"], "askable": False,
                         "error": f"{type(error).__name__}: {error}"})
            continue
        rows.append({
            "id": entry["id"], "name": entry["name"], "kind": entry["kind"], "city": entry["city"],
            "headline": entry["headline"], "claim_level": entry["claim_level"],
            "will_not_publish": entry["will_not_publish"], "feature_count": entry["feature_count"],
            "sample": entry["sample"], "askable": askable,
            "opened": result.get("opened", False), "rejection": result.get("rejection"),
            "overlap_basis_points": result["overlap_basis_points"],
            "overlap_ratio": result["overlap_ratio"],
            "matched_features": result["matched_features"],
            "passed_floor": result["passed_floor"], "agreed": result["agreed"],
            "wires_clean": result["leak_check"]["clean"],
            "evidence_ids": {name: envelope["evidence_id"]
                             for name, envelope in result["evidence"].items()},
        })
    # The sift. This is what makes a room of thousands searchable at all: an exchange is one full private
    # negotiation, so a search runs them with the agents that share a bucket, not with everyone. An agent
    # whose profile is in another bucket space, or who publishes no profile, cannot be sifted: it is asked,
    # and the cost of that is stated in the answer instead of being hidden.
    seeker = seeker_buckets(my_features)
    space_here = bucket_space_name()
    sifted = skipped = unsifted = 0
    skipped_ids: list[str] = []
    for remote in remotes_to_check():
        judged = False
        if seeker and space_here and remote.get("bucket_space") == space_here:
            try:
                theirs = json.loads(remote.get("profile") or "{}")
            except json.JSONDecodeError:
                theirs = {}
            their_buckets = theirs.get("buckets") if isinstance(theirs, dict) else None
            their_items = int((theirs or {}).get("feature_count") or 0)
            if isinstance(their_buckets, dict) and their_buckets:
                sifted += 1
                judged = True
                if not [bucket for bucket in their_buckets if seeker.get(bucket)]:
                    skipped += 1
                    skipped_ids.append(remote["id"])
                    continue
            elif their_items:
                # It holds items and placed none of them in our published space. We cannot judge it, so we
                # must not skip it: an unplaceable list is not an empty one.
                pass
        if not judged and remote["active"]:
            unsifted += 1
        try:
            result = run_remote_exchange(
                my_features, remote["endpoint"], floor, remote_id=remote["name"],
                remote_kind=remote["kind"], my_kind=my_kind,
                peer_client=peer_client_for(remote),
            )
        except Exception as error:  # noqa: BLE001
            # Log the shape of the failure as well as its name: a row that says "unreachable" and nothing more
            # cannot be told apart from a protocol mistake on our own side, which this one turned out to be.
            import traceback

            print(f"exchange with {remote['id']} failed: {type(error).__name__}: {error}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            withdrawn = note_remote_failure(remote["id"], f"{type(error).__name__}: {error}")
            result = {"id": remote["id"], "name": remote["name"], "kind": remote["kind"],
                      "city": remote["city"], "headline": remote["headline"],
                      "claim_level": "declared", "will_not_publish": "",
                      "feature_count": None, "sample": False, "remote": True,
                      "opened": False, "rejection": f"remote_unreachable: {type(error).__name__}",
                      "overlap_basis_points": None, "overlap_ratio": None,
                      "matched_features": [], "passed_floor": False, "agreed": False,
                      "wires_clean": True, "leaked_features": [], "evidence_ids": {},
                      "withdrawn": withdrawn}
        # The row is identified by the id in the store, not by the name the other side publishes:
        # a caller acts on this row, and the name belongs to the remote agent, not to us.
        result["id"] = remote["id"]
        # And it carries the door, like every other row does: a remote agent can be asked, whether it is reached
        # at an address or through the poll it holds open. Without this the page draws no ask button and the
        # person cannot do what the row says is possible.
        result["askable"] = bool(remote["active"])
        # And how it is reached, so the answer does not make a polled agent look like one with an address.
        result["pull"] = (remote.get("mode") or "address") == "pull"
        if result.get("answered", True):
            note_remote_success(remote["id"])
        else:
            # Nothing came back. Say that plainly in the row: a reader must be able to tell an absent
            # agent from one that answered and refused, because only the first one leaves the room.
            reason = str(result.get("rejection") or "no answer").replace("remote_refused: ", "")
            result["rejection"] = f"remote_unreachable: {reason}"
            result["withdrawn"] = note_remote_failure(remote["id"], result["rejection"])
        result["city"] = remote["city"] or result.get("city", "")
        result["headline"] = remote["headline"] or result.get("headline", "")
        if looks_like_own_agent(result, my_features):
            result["possible_self_match"] = True
        if remote["active"]:
            rows.append(result)

    rows.sort(key=lambda row: (row.get("overlap_basis_points") or -1), reverse=True)
    return {
        "roster_kind": "samples, registered and connected",
        "floor_basis_points": floor,
        "your_kind": my_kind,
        "agents_checked": len(rows),
        "registered_agents": sum(1 for row in rows if not row.get("sample") and not row.get("remote")),
        "remote_sifted": sifted, "remote_skipped_by_bucket": skipped, "remote_unsifted": unsifted,
        "skipped_ids": skipped_ids,
        "sift_note": "An exchange is one full private negotiation per agent, so a search runs them with the "
                     "agents that share a bucket with you. Skipped agents were not asked; unsifted ones were "
                     "asked because they do not publish a comparable profile.",
        "connected_agents": sum(1 for row in rows if row.get("remote")),
        "above_floor": sum(1 for row in rows if row.get("passed_floor")),
        "not_comparable": sum(1 for row in rows if not row.get("opened")),
        "wires_clean": all(row.get("wires_clean", False) for row in rows),
        "your_feature_count": len(my_features),
        "rows": rows,
    }


INTAKE_KEY_ENV = "ARLI_INTAKE_KEY"
TAXONOMY_PATH_ENV = "ARLI_MATCH_TAXONOMY"
DEFAULT_TAXONOMY_PATH = "/llm-gateway/skill_taxonomy_v2.json"


VOCABULARY_ID = "arli-areas-v1"


def vocabulary() -> dict:
    """The areas this room indexes in, published to whoever may hold them.

    An agent can only be sifted if it can name the areas it works in, and a foreign agent has no copy of the
    vocabulary. Handing it over is deliberate and limited: the vocabulary is the licensed data of the network,
    so it travels to a holder of the room's key rather than to anyone who asks, and this endpoint says so
    instead of answering an empty object.
    """
    path = os.environ.get(TAXONOMY_PATH_ENV, DEFAULT_TAXONOMY_PATH)
    if not (path and Path(path).is_file()):
        return {"error": "no_vocabulary", "detail": "this deployment indexes no vocabulary"}
    taxonomy_ = taxonomy()
    areas: dict[str, str] = {}
    for slug, node in taxonomy_.entries.items():
        areas[node["category"]] = node["category"]
    return {"vocabulary": VOCABULARY_ID, "size": len(areas), "areas": sorted(areas),
            "items": taxonomy_.size(),
            "note": "Areas only: the item names that map onto them are the licensed part and are not here. "
                    "An agent that holds them resolves items itself; one that does not declares areas by hand."}


def bucket_space_name() -> str:
    """The name of the bucket space this room indexes in, spelled exactly as an agent spells it.

    Two bucket spaces are not comparable: a shared bucket means nothing across them, and "no shared bucket"
    across two spaces is not evidence of no match. An agent publishing in another space is therefore never
    skipped on its profile, only asked, and the room says so in the answer.
    """
    path = os.environ.get(TAXONOMY_PATH_ENV, DEFAULT_TAXONOMY_PATH)
    return VOCABULARY_ID if path and Path(path).is_file() else ""


def seeker_buckets(features: list[str]) -> dict[str, int]:
    """What the seeker's list looks like coarsely, in this room's own space. Never published."""
    taxonomy_ = taxonomy()
    profile: dict[str, int] = {}
    for feature in features:
        slug = feature.split(":", 1)[-1]
        category = taxonomy_.category_of(slug)
        if category:
            profile[category] = profile.get(category, 0) + 1
    return profile


def taxonomy() -> Taxonomy:
    """The published taxonomy, loaded once, from a file the deployment names.

    The production file is the company's licensed data and does not live in this repository, so the path is
    configuration and its absence is not an error: without it the intake works on the small built-in set,
    and says which set it used in every answer.
    """
    path = os.environ.get(TAXONOMY_PATH_ENV, DEFAULT_TAXONOMY_PATH)
    if path and Path(path).is_file():
        return Taxonomy.from_file(path)
    return Taxonomy.built_in()


def intake_allowed(header_value: str | None) -> bool:
    """Whether this caller may create an agent from written words.

    The door is not open to the network. Creating an agent out of a sentence is something a signed-in person
    does in the app, where the agent has an owner from the first moment; an anonymous endpoint would leave
    unowned agents in the room and put the store behind a free entrance. A missing key closes the door rather
    than opening it, so a deployment that forgets to configure one fails closed.
    """
    expected = os.environ.get(INTAKE_KEY_ENV, "").strip()
    if not expected:
        return False
    return secrets.compare_digest(str(header_value or "").strip(), expected)


def intake(payload: dict) -> dict:
    """A person writes what they need; they get an agent and can search immediately.

    This is the door for somebody who has no agent, no server and no vocabulary. What comes back is the
    reading of their words, everything the reading could not place, and the agent that was created from it.
    Nothing is invented: if no feature could be placed, no agent is created and they are asked to say it
    another way, because an agent with an empty list would match nothing and look like a working search.
    """
    text = str(payload.get("text") or "").strip()
    if len(text) < 3:
        raise ValueError("write what you need in your own words, a few words at least")
    read = read_request(text, taxonomy=taxonomy(), kind=str(payload.get("kind") or "") or None)
    if not read["features"]:
        return {
            "created": False,
            "read": read,
            "note": "Nothing in that could be placed, so no agent was created: an agent with an empty "
                    "list would match nothing and look like a search that found nothing.",
        }

    name = str(payload.get("name") or "").strip() or text.split("\n")[0][:60]
    agent, token = create_agent({
        "name": name,
        "kind": read["kind"] or "demand",
        "city": read["city"],
        "headline": text[:140],
        "claim_level": "declared",
        "will_not_publish": "the list this reading produced",
        "features": read["features"],
    })
    return {
        "created": True,
        "agent": agent,
        "token": token,
        "manage_url": f"/account?token={token}",
        "read": read,
        "keep_this": "The token is the only key to this agent. It is not stored anywhere readable and is "
                     "never shown again.",
        "note": "The agent exists now, hosted here, and its list is the reading of your words. Give it an "
                "address of your own later and it moves, keeping its identity.",
    }


# --- accounts on this service, for people who have no agent -----------------------------------------
# Why a separate account system here rather than the platform's: this is the matching product, its app is
# this site, and a person who registers to get an agent should land on the page that makes one. Two products,
# two doors, one network underneath.
SESSION_COOKIE = "arli_match_session"
SESSION_DAYS = 30
MIN_PASSWORD_CHARS = 8
ATTEMPT_WINDOW_SECONDS = 600
ATTEMPTS_ALLOWED = 8
# A search is the expensive public call: one closed exchange per agent that shares a bucket. Left open, one
# address can burn the machine for everybody, and the cost is charged to us rather than to the caller.
SEARCH_ATTEMPTS_ALLOWED = int(os.environ.get("ARLI_MATCH_SEARCH_ATTEMPTS") or 30)
SEARCH_WINDOW_SECONDS = int(os.environ.get("ARLI_MATCH_SEARCH_WINDOW") or 600)
_attempts: dict = {}

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_password(password: str) -> str:
    """scrypt with a per-account salt, from the standard library, so no password is ever stored, only its cost."""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, digest_hex = str(stored).split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=2 ** 14, r=8, p=1, dklen=32)
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(digest.hex(), digest_hex)


def _clean_email(value) -> str:
    email = str(value or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise ValueError("that does not look like an email address")
    return email


def client_ip(peer: str, forwarded: str = "") -> str:
    """Who to count, when a proxy stands in front.

    Behind nginx every request arrives from loopback, so counting the connection address makes one limit shared by
    everybody: it would either never fire or fire on the whole world. The forwarded address is used when the peer
    is loopback, because that is the only case where the header was put there by our own proxy rather than by a
    stranger talking to us directly.
    """
    peer = peer or "unknown"
    if peer in ("127.0.0.1", "::1", "localhost") and forwarded.strip():
        first = forwarded.split(",")[0].strip()
        if first:
            return first
    return peer


def attempts_allowed(bucket: str, allowed: int | None = None, window: int | None = None) -> bool:
    """A blunt limit on registering and signing in: enough for a person, too little for a script."""
    now = time.time()
    span = ATTEMPT_WINDOW_SECONDS if window is None else window
    cap = ATTEMPTS_ALLOWED if allowed is None else allowed
    recent = [moment for moment in _attempts.get(bucket, []) if now - moment < span]
    _attempts[bucket] = recent
    return len(recent) < cap


def note_attempt(bucket: str) -> None:
    _attempts.setdefault(bucket, []).append(time.time())


def _open_session(connection: sqlite3.Connection, account_id: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    connection.execute("INSERT INTO sessions (token, account_id, created_at, expires_at) VALUES (?,?,?,?)",
                       (token, account_id, _now(), expires))
    return token


def _stored_hash(account_id: str) -> str:
    with connect() as connection:
        row = connection.execute("SELECT password_hash FROM accounts WHERE id = ?", (account_id,)).fetchone()
    return str(row["password_hash"]) if row else ""


def account_by_session(token: str) -> dict | None:
    """Who is signed in, from the cookie and nothing else. An expired or unknown token is nobody."""
    if not token:
        return None
    with connect() as connection:
        row = connection.execute(
            """SELECT a.id AS id, a.email AS email, a.kid AS kid, a.public_jwk AS public_jwk
               FROM sessions AS s JOIN accounts AS a ON a.id = s.account_id
               WHERE s.token = ? AND s.expires_at > ?""",
            (token, _now()),
        ).fetchone()
    return dict(row) if row else None


def account_keypair() -> tuple[str, dict, str]:
    """A signing key per account, so what a person agreed to is checkable by somebody who was not there.

    The private key is kept by the service, in the same store as everything else. That is a real trade and it
    is said out loud rather than hidden: an account hosted here is held here, key and vault alike. Move to
    your own agent later and the key is yours.
    """
    from arli_engine.core.signing import KeyPair, public_jwk

    pair = KeyPair.generate()
    kid = f"kid-{secrets.token_hex(4)}"
    return kid, public_jwk(pair.public_pem, kid), pair.private_pem.decode("utf-8")


def register_account(email, password) -> tuple[dict, str]:
    email = _clean_email(email)
    password = str(password or "")
    if len(password) < MIN_PASSWORD_CHARS:
        raise ValueError(f"a password of at least {MIN_PASSWORD_CHARS} characters")
    account_id = f"u-{secrets.token_hex(6)}"
    kid, jwk, private_pem = account_keypair()
    with connect() as connection:
        if connection.execute("SELECT id FROM accounts WHERE email = ?", (email,)).fetchone():
            raise ValueError("an account with this email already exists: sign in instead")
        connection.execute(
            "INSERT INTO accounts (id, email, password_hash, created_at, kid, public_jwk, private_pem)"
            " VALUES (?,?,?,?,?,?,?)",
            (account_id, email, _hash_password(password), _now(), kid,
             json.dumps(jwk), private_pem))
        token = _open_session(connection, account_id)
    return {"id": account_id, "email": email, "kid": kid, "public_jwk": jwk}, token


def sign_in(email, password) -> tuple[dict | None, str]:
    """An unknown email and a wrong password give the same answer, on purpose: which one was wrong is not
    something a stranger gets to learn."""
    email = str(email or "").strip().lower()
    with connect() as connection:
        row = connection.execute("SELECT * FROM accounts WHERE email = ?", (email,)).fetchone()
        if row is None or not _verify_password(str(password or ""), row["password_hash"]):
            return None, ""
        token = _open_session(connection, row["id"])
    return {"id": row["id"], "email": row["email"]}, token


def sign_out(token: str) -> None:
    if not token:
        return
    with connect() as connection:
        connection.execute("DELETE FROM sessions WHERE token = ?", (token,))


def close_account(account_id: str) -> dict:
    """Close the account and take its agents out of the room. Nothing is left behind for the room to see."""
    with connect() as connection:
        agents = [row["id"] for row in connection.execute(
            "SELECT id FROM agents WHERE owner_account = ?", (account_id,)).fetchall()]
        connection.execute("UPDATE agents SET active = 0 WHERE owner_account = ?", (account_id,))
        removed = connection.execute("DELETE FROM sessions WHERE account_id = ?", (account_id,)).rowcount
        connection.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    return {"agents_removed": len(agents), "sessions_removed": removed}


def app_agents(account_id: str) -> list[dict]:
    """The agents this account made, with the link that manages each one and nothing of their lists."""
    with connect() as connection:
        rows = connection.execute(
            """SELECT id, name, kind, city, headline, features, owner_token, created_at
               FROM agents WHERE owner_account = ? AND active = 1 ORDER BY created_at DESC""",
            (account_id,),
        ).fetchall()
    return [{
        "id": row["id"], "name": row["name"], "kind": row["kind"], "city": row["city"],
        "headline": row["headline"], "feature_count": len(json.loads(row["features"])),
        "manage_url": f"https://match.arliwork.com/account?token={row['owner_token']}",
        "created_at": row["created_at"],
    } for row in rows]


# --- introductions: a match is not a conversation, and this is the step between -------------------------
# What happens after a search finds somebody. A match is a number; two sides still have to agree to be put in
# touch, and each approves separately. Nothing is shared before both have: the contact opens only when the
# other side has also said yes. Every step writes an evidence envelope, so afterwards either side can point
# at what was decided and when, rather than at a copy of a chat.
#
# What this does not do yet, and it is said rather than hidden: an agent not hosted here is not reachable by
# this path. Its introduction would have to travel to its own address with arli/opportunity/decide, which is
# the same two-sided rule over the wire, and that step is not built.

INTRO_STATES = ("requested", "declined", "introduced", "withdrawn")


def _evidence(operation: str, parameters: dict, result: dict, authority: str = "local-calculation") -> dict:
    from arli_engine.core.evidence import Evidence

    envelope = Evidence(
        provider={"service": "arli-match", "product": "match.arliwork.com"},
        operation=operation,
        parameters=parameters,
        normalized_result=result,
        confidence="observed",
        authority=authority,
        limitations=["The service records that a side said this, and nothing about whether it was true."],
    )
    return envelope.to_json()


def _record_evidence(connection: sqlite3.Connection, intro_id: str, envelope: dict) -> None:
    row = connection.execute("SELECT evidence_ids FROM introductions WHERE id = ?", (intro_id,)).fetchone()
    ids = json.loads(row["evidence_ids"]) if row else []
    ids.append(envelope["evidence_id"])
    connection.execute("UPDATE introductions SET evidence_ids = ? WHERE id = ?", (json.dumps(ids), intro_id))


class IntroductionRefused(RuntimeError):
    """The introduction was refused, with a code from a small closed list."""

    CODES = ("peer_not_hosted_here", "same_owner", "already_asked", "unknown_introduction",
             "not_yours_to_decide", "already_decided", "not_introduced_yet", "empty_message")

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in self.CODES:
            raise ValueError(f"{code!r} is not in IntroductionRefused.CODES")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _agent_owner(connection: sqlite3.Connection, agent_id: str) -> str:
    row = connection.execute("SELECT owner_account, active FROM agents WHERE id = ?", (agent_id,)).fetchone()
    if row is None or not row["active"]:
        return ""
    return str(row["owner_account"] or "")


def request_introduction(account: dict, peer_agent_id: str, note: str = "") -> dict:
    """One side asks to be put in touch. Order matters: the other side learns nothing until it answers."""
    peer_agent_id = str(peer_agent_id or "").strip()
    if not peer_agent_id:
        raise IntroductionRefused("unknown_introduction", "name the agent you want to be introduced to")
    with connect() as connection:
        peer_owner = _agent_owner(connection, peer_agent_id)
        peer_remote = None
        if not peer_owner:
            row = connection.execute(
                "SELECT * FROM remotes WHERE id = ? AND active = 1", (peer_agent_id,)).fetchone()
            peer_remote = dict(row) if row else None
        if not peer_owner and peer_remote is None:
            raise IntroductionRefused(
                "peer_not_hosted_here",
                "this agent is not in the room: it is either an example with no owner, or it was never "
                "connected. An introduction needs either an account here or an address to reach it at.",
            )
        if peer_owner == account["id"]:
            raise IntroductionRefused("same_owner", "that agent is yours: an agent is not introduced to its own owner")
        mine = connection.execute(
            "SELECT id FROM agents WHERE owner_account = ? AND active = 1 ORDER BY created_at LIMIT 1",
            (account["id"],)).fetchone()
        existing = connection.execute(
            """SELECT id, state FROM introductions
               WHERE from_account = ? AND to_agent = ? AND state IN ('requested','introduced')
               ORDER BY created_at DESC LIMIT 1""",
            (account["id"], peer_agent_id)).fetchone()
        if existing:
            raise IntroductionRefused("already_asked",
                                      f"you already asked for this introduction: it is {existing['state']}")
        mine = mine or connection.execute(
            "SELECT id FROM agents WHERE owner_account = ? AND active = 1 ORDER BY created_at LIMIT 1",
            (account["id"],)).fetchone()

        if peer_remote is not None:
            # An agent living at its own address: the introduction travels there as a negotiation, and its
            # journal records what we said. The contact is that address, because that is what it has.
            from app.network import run_remote_introduction

            wire = run_remote_introduction(mine["id"] if mine else account["email"], peer_agent_id,
                                           peer_remote["endpoint"], note,
                                           remote_kind=str(peer_remote.get("kind") or "supply"),
                                           peer_client=peer_client_for(peer_remote))
            reach = "poll" if (peer_remote.get("mode") or "address") == "pull" else "address"
            intro_id = f"in-{secrets.token_hex(6)}"
            state = "introduced" if wire.get("agreed") else "declined"
            envelope = _evidence("introduction.over_the_wire",
                                 {"introduction_id": intro_id, "to_agent": peer_agent_id,
                                  "endpoint": peer_remote["endpoint"], "negotiation_id": wire.get("session", ""),
                                  "note": note.strip()[:400]},
                                 {"state": state, "outcome": wire.get("outcome"),
                                  "decided_by": wire.get("decided_by"), "reached_by": reach},
                                 authority="self-declared")
            connection.execute(
                """INSERT INTO introductions (id, from_account, from_agent, to_agent, to_account, note, state,
                   created_at, decided_at, opened_at, negotiation_id, remote_endpoint, evidence_ids)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (intro_id, account["id"], mine["id"] if mine else "", peer_agent_id, "", note.strip()[:400],
                 state, _now(), _now(), _now() if state == "introduced" else "",
                 wire.get("session", ""), peer_remote["endpoint"],
                 json.dumps([envelope["evidence_id"]]
                            + [identifier for identifier in (wire.get("evidence_ids") or []) if identifier])))
            return {
                "id": intro_id, "state": state, "to_agent": peer_agent_id,
                "over_the_wire": True, "endpoint": peer_remote["endpoint"], "reached_by": reach,
                "negotiation_id": wire.get("session", ""),
                "rejection": wire.get("rejection") or wire.get("detail"),
                "evidence_id": envelope["evidence_id"],
                "note": ("Its agent answered for its own seat, and its own journal now records what we said. "
                         "Our side keeps its own copy of the same trail."),
            }

        intro_id = f"in-{secrets.token_hex(6)}"
        envelope = _evidence("introduction.requested",
                             {"introduction_id": intro_id, "from_agent": mine["id"] if mine else "",
                              "to_agent": peer_agent_id, "note": note.strip()[:400]},
                             {"state": "requested"})
        connection.execute(
            """INSERT INTO introductions (id, from_account, from_agent, to_agent, to_account, note, state,
               created_at, evidence_ids) VALUES (?,?,?,?,?,?,?,?,?)""",
            (intro_id, account["id"], mine["id"] if mine else "", peer_agent_id, peer_owner,
             note.strip()[:400], "requested", _now(), json.dumps([envelope["evidence_id"]])))
    return {"id": intro_id, "state": "requested", "to_agent": peer_agent_id,
            "evidence_id": envelope["evidence_id"]}


def decide_introduction(account: dict, intro_id: str, approve: bool, message: str = "") -> dict:
    """The other side answers. Only then does either side learn how to reach the other."""
    with connect() as connection:
        row = connection.execute("SELECT * FROM introductions WHERE id = ?", (intro_id,)).fetchone()
        if row is None:
            raise IntroductionRefused("unknown_introduction", f"no introduction with the id {intro_id}")
        if row["to_account"] != account["id"]:
            raise IntroductionRefused("not_yours_to_decide", "this introduction was not addressed to you")
        if row["state"] != "requested":
            raise IntroductionRefused("already_decided", f"this one is already {row['state']}")
        state = "introduced" if approve else "declined"
        envelope = _evidence("introduction.decided" if approve else "introduction.declined",
                             {"introduction_id": intro_id, "decided_by": account["email"],
                              "message": message.strip()[:400]},
                             {"state": state}, authority="self-declared")
        connection.execute(
            "UPDATE introductions SET state = ?, decided_at = ?, opened_at = ? WHERE id = ?",
            (state, _now(), _now() if approve else "", intro_id))
        _record_evidence(connection, intro_id, envelope)
        if approve and message.strip():
            _store_message(connection, intro_id, account, message)
    return {"id": intro_id, "state": state, "evidence_id": envelope["evidence_id"]}


def _store_message(connection: sqlite3.Connection, intro_id: str, account: dict, body: str) -> dict:
    body = str(body or "").strip()
    if not body:
        raise IntroductionRefused("empty_message", "an empty message is not a message")
    message_id = f"m-{secrets.token_hex(6)}"
    envelope = _evidence("introduction.message",
                         {"introduction_id": intro_id, "author": account["email"], "body": body[:400]},
                         {"stored": True}, authority="self-declared")
    connection.execute(
        "INSERT INTO intro_messages (id, introduction_id, author, body, created_at, evidence_id) VALUES (?,?,?,?,?,?)",
        (message_id, intro_id, account["email"], body[:2000], _now(), envelope["evidence_id"]))
    _record_evidence(connection, intro_id, envelope)
    return {"id": message_id, "evidence_id": envelope["evidence_id"]}


def send_introduction_message(account: dict, intro_id: str, body: str) -> dict:
    with connect() as connection:
        row = connection.execute("SELECT * FROM introductions WHERE id = ?", (intro_id,)).fetchone()
        if row is None or account["id"] not in (row["from_account"], row["to_account"]):
            raise IntroductionRefused("unknown_introduction", f"no introduction with the id {intro_id} for you")
        if row["state"] != "introduced":
            raise IntroductionRefused("not_introduced_yet",
                                      f"this one is {row['state']}: messages start once both sides have agreed")
        return _store_message(connection, intro_id, account, body)


def introduction_view(account: dict, row) -> dict:
    """What one side is allowed to see. The contact and the messages appear only after both said yes."""
    mine_to_receive = row["to_account"] == account["id"]
    peer_agent = row["to_agent"] if not mine_to_receive else row["from_agent"]
    out = {
        "id": row["id"], "state": row["state"], "created_at": row["created_at"],
        "decided_at": row["decided_at"], "direction": "incoming" if mine_to_receive else "outgoing",
        "agent": peer_agent, "note": row["note"],
        "evidence_ids": json.loads(row["evidence_ids"] or "[]"),
        "yours_to_decide": mine_to_receive and row["state"] == "requested",
    }
    if row["state"] != "introduced":
        out["contact"] = None
        out["note_on_contact"] = ("The contact appears when both sides have agreed. Until then the other side "
                                  "is a matching agent and nothing more.")
        return out
    if not row["to_account"] and not row["remote_endpoint"]:
        # An agent registered by calling the room has no address to show, and that must be said rather than
        # left as an empty contact: it is reachable, just not by dialling it.
        with connect() as connection:
            reached = connection.execute("SELECT mode FROM remotes WHERE id = ? AND active = 1",
                                         (row["to_agent"],)).fetchone()
        if reached is not None and (reached["mode"] or "address") == "pull":
            out["contact"] = {
                "agent": row["to_agent"], "reach": "poll",
                "how": "an agent with no address: it calls the room and answers on its next poll",
            }
            out["negotiation_id"] = row["negotiation_id"]
            out["note_on_contact"] = (
                "This agent has no address to dial. The introduction was carried over the protocol through the "
                "poll it holds open, and its own journal records our proposal. Write to it and it answers on "
                "its next poll; there is no email, because it has none to give."
            )
            return out
    if not row["to_account"] and row["remote_endpoint"]:
        out["contact"] = {
            "endpoint": row["remote_endpoint"], "agent": row["to_agent"],
            "how": "an agent at its own address, not hosted here",
        }
        out["negotiation_id"] = row["negotiation_id"]
        out["note_on_contact"] = (
            "This agent lives at its own address. The introduction was carried to it over the protocol and "
            "its agent answered for its own seat; its journal records our proposal. There is no email to "
            "show, because there is none on our side to show."
        )
        out["messages"] = []
        return out
    peer_account = row["to_account"] if not mine_to_receive else row["from_account"]
    with connect() as connection:
        peer = connection.execute(
            """SELECT a.email AS email, g.name AS name, g.headline AS headline, g.kind AS kind, g.city AS city
               FROM accounts AS a LEFT JOIN agents AS g ON g.owner_account = a.id AND g.active = 1
               WHERE a.id = ? LIMIT 1""", (peer_account,)).fetchone()
        messages = connection.execute(
            "SELECT author, body, created_at, evidence_id FROM intro_messages WHERE introduction_id = ? "
            "ORDER BY created_at", (row["id"],)).fetchall()
    out["contact"] = dict(peer) if peer else None
    out["messages"] = [dict(message) for message in messages]
    return out


def introductions_for(account: dict) -> dict:
    with connect() as connection:
        rows = connection.execute(
            """SELECT * FROM introductions WHERE from_account = ? OR to_account = ?
               ORDER BY created_at DESC LIMIT 100""", (account["id"], account["id"])).fetchall()
    views = [introduction_view(account, row) for row in rows]
    return {"incoming": [v for v in views if v["direction"] == "incoming"],
            "outgoing": [v for v in views if v["direction"] == "outgoing"]}


# --- agreements: what the two sides agreed to, in a form somebody else can check ----------------------
# Why this exists. An introduction opens a conversation, and a conversation ends in "we agreed" that neither
# side can point at afterwards. An agreement here is a small set of stated terms, signed by both accounts,
# with the exact bytes that were signed kept beside it, so a stranger holding the id can check the two
# signatures himself instead of believing a screenshot. Every proposal, answer and milestone leaves an
# evidence envelope as well.
#
# What this is not, and it is said rather than implied: it moves no money and decides nothing legally. It is
# a record of what was said, checkable. Anything with value in it still needs a contract, a payment rail and
# a decision from the founder, and none of those are here.

AGREEMENT_STATES = ("proposed", "agreed", "declined", "superseded")
AGREEMENT_EVENT_KINDS = ("delivered", "accepted", "issue")


class AgreementRefused(RuntimeError):
    CODES = ("unknown_introduction", "not_a_party", "not_introduced_yet", "subject_required",
             "amount_needs_currency", "currency_needs_amount", "bad_date", "already_agreed",
             "not_yours_to_decide", "unknown_agreement", "unknown_event_kind", "empty_note",
             "not_agreed_yet")

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in self.CODES:
            raise ValueError(f"{code!r} is not in AgreementRefused.CODES")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _store_envelope(connection: sqlite3.Connection, subject: str, envelope: dict) -> None:
    """Kept whole, not just its id: an id can be recomputed from an envelope, and only then is a check a check."""
    connection.execute(
        "INSERT OR REPLACE INTO evidence_records (id, subject, envelope, created_at) VALUES (?,?,?,?)",
        (envelope["evidence_id"], subject, json.dumps(envelope), _now()))


def _clean_terms(payload: dict) -> dict:
    subject = str(payload.get("subject") or "").strip()
    if len(subject) < 3:
        raise AgreementRefused("subject_required", "say what this agreement is about, in one line")
    terms: dict = {"subject": subject[:300]}
    deliver_by = str(payload.get("deliver_by") or "").strip()
    if deliver_by:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", deliver_by):
            raise AgreementRefused("bad_date", "a date looks like 2026-11-01")
        terms["deliver_by"] = deliver_by
    for field in ("place", "notes"):
        value = str(payload.get(field) or "").strip()
        if value:
            terms[field] = value[:600]
    amount = payload.get("amount_minor")
    currency = str(payload.get("currency") or "").strip().upper()
    if amount is not None and str(amount).strip() != "":
        if not currency:
            raise AgreementRefused("amount_needs_currency", "an amount without a currency is not an amount")
        try:
            value = int(amount)
        except (TypeError, ValueError):
            raise AgreementRefused("amount_needs_currency", "the amount must be a whole number of minor units")
        if value < 0:
            raise AgreementRefused("amount_needs_currency", "a negative amount is not an amount")
        terms["amount_minor"] = value
        terms["currency"] = currency[:3]
    elif currency:
        raise AgreementRefused("currency_needs_amount", "a currency without an amount says nothing")
    return terms


def _agreement_body(introduction_id: str, version: int, proposer_email: str, terms: dict) -> bytes:
    """The exact bytes both sides sign. Kept beside the record, because a signature over other bytes is not
    this signature, and a stranger has to be able to rebuild these bytes to check that."""
    from arli_engine.core.canonical import canonical_json

    return canonical_json({"introduction": introduction_id, "version": version,
                           "proposed_by": proposer_email, "terms": terms}).encode("utf-8")


def _introduction_parties(connection: sqlite3.Connection, introduction_id: str):
    row = connection.execute("SELECT * FROM introductions WHERE id = ?", (introduction_id,)).fetchone()
    if row is None:
        raise AgreementRefused("unknown_introduction", f"no introduction with the id {introduction_id}")
    return row


def _account_key(connection: sqlite3.Connection, account_id: str) -> tuple[dict, str]:
    row = connection.execute(
        "SELECT kid, public_jwk, private_pem FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise AgreementRefused("not_a_party", "no such account")
    return json.loads(row["public_jwk"] or "{}"), str(row["private_pem"] or "")


def _jwk_of(account: dict) -> dict:
    """The public key of an account, whether it arrives as a stored string or as a freshly built dict."""
    raw = account.get("public_jwk")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


def propose_agreement(account: dict, introduction_id: str, payload: dict) -> dict:
    """Put terms on the table. A second proposal from the other side replaces this one and is a counter, which
    is kept: the sequence is what the two sides did, not only where they landed."""
    from arli_engine.core.signing import sign_bytes

    terms = _clean_terms(payload)
    with connect() as connection:
        introduction = _introduction_parties(connection, introduction_id)
        if account["id"] not in (introduction["from_account"], introduction["to_account"]):
            raise AgreementRefused("not_a_party", "this introduction is not yours")
        if introduction["state"] != "introduced":
            raise AgreementRefused("not_introduced_yet",
                                   "terms come after both sides agreed to be introduced")
        agreed = connection.execute(
            "SELECT id FROM agreements WHERE introduction_id = ? AND state = 'agreed'", (introduction_id,)).fetchone()
        if agreed:
            raise AgreementRefused("already_agreed",
                                   f"this pair already has an agreed record: {agreed['id']}. A change means a "
                                   "new proposal, which means talking again")
        previous = connection.execute(
            "SELECT id, version FROM agreements WHERE introduction_id = ? ORDER BY version DESC LIMIT 1",
            (introduction_id,)).fetchone()
        version = (previous["version"] + 1) if previous else 1
        if previous:
            connection.execute("UPDATE agreements SET state = 'superseded' WHERE id = ?", (previous["id"],))

        body = _agreement_body(introduction_id, version, account["email"], terms)
        _jwk, private_pem = _account_key(connection, account["id"])
        signature = sign_bytes(private_pem.encode("utf-8"), str(account.get("kid") or ""), body)
        signature["jwk"] = _jwk_of(account) or signature.get("jwk")
        agreement_id = f"ag-{secrets.token_urlsafe(12)}"
        envelope = _evidence("agreement.proposed",
                             {"agreement_id": agreement_id, "introduction_id": introduction_id,
                              "version": version, "subject": terms["subject"]},
                             {"state": "proposed"}, authority="self-declared")
        connection.execute(
            """INSERT INTO agreements (id, introduction_id, version, proposed_by, proposed_by_email, terms,
               body, state, created_at, signature_proposer, evidence_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (agreement_id, introduction_id, version, account["id"], account["email"], json.dumps(terms),
             body.decode("utf-8"), "proposed", _now(), json.dumps(signature),
             json.dumps([envelope["evidence_id"]])))
        _store_envelope(connection, agreement_id, envelope)
    return {"id": agreement_id, "version": version, "state": "proposed", "terms": terms,
            "signed_by_you": True, "evidence_id": envelope["evidence_id"]}


def decide_agreement(account: dict, agreement_id: str, accept: bool, note: str = "") -> dict:
    """The other side answers the proposal. Accepting means signing the same bytes the proposer signed."""
    from arli_engine.core.signing import sign_bytes

    with connect() as connection:
        row = connection.execute("SELECT * FROM agreements WHERE id = ?", (agreement_id,)).fetchone()
        if row is None:
            raise AgreementRefused("unknown_agreement", f"no agreement with the id {agreement_id}")
        introduction = _introduction_parties(connection, row["introduction_id"])
        if account["id"] not in (introduction["from_account"], introduction["to_account"]):
            raise AgreementRefused("not_a_party", "this record is not yours")
        if account["id"] == row["proposed_by"]:
            raise AgreementRefused("not_yours_to_decide", "the side that proposed does not answer its own proposal")
        if row["state"] != "proposed":
            raise AgreementRefused("unknown_agreement", f"this one is already {row['state']}")

        state = "agreed" if accept else "declined"
        operation = "agreement.agreed" if accept else "agreement.declined"
        envelope = _evidence(operation,
                             {"agreement_id": agreement_id, "introduction_id": row["introduction_id"],
                              "decided_by": account["email"], "note": str(note or "")[:400]},
                             {"state": state}, authority="self-declared")
        signature_json = ""
        if accept:
            _jwk, private_pem = _account_key(connection, account["id"])
            signature = sign_bytes(private_pem.encode("utf-8"), str(account.get("kid") or ""),
                                   row["body"].encode("utf-8"))
            signature["jwk"] = _jwk_of(account) or signature.get("jwk")
            signature_json = json.dumps(signature)
        connection.execute(
            "UPDATE agreements SET state = ?, accepted_by = ?, accepted_by_email = ?, accepted_at = ?,"
            " signature_acceptor = ? WHERE id = ?",
            (state, account["id"], account["email"], _now() if accept else "", signature_json, agreement_id))
        ids = json.loads(row["evidence_ids"] or "[]") + [envelope["evidence_id"]]
        connection.execute("UPDATE agreements SET evidence_ids = ? WHERE id = ?", (json.dumps(ids), agreement_id))
        _store_envelope(connection, agreement_id, envelope)
    return {"id": agreement_id, "state": state, "evidence_id": envelope["evidence_id"]}


def record_agreement_event(account: dict, agreement_id: str, kind: str, note: str = "") -> dict:
    """What happened next: delivered, accepted, or a problem stated. Only after there is something to run."""
    kind = str(kind or "").strip().lower()
    if kind not in AGREEMENT_EVENT_KINDS:
        raise AgreementRefused("unknown_event_kind", f"one of {', '.join(AGREEMENT_EVENT_KINDS)}")
    with connect() as connection:
        row = connection.execute("SELECT * FROM agreements WHERE id = ?", (agreement_id,)).fetchone()
        if row is None:
            raise AgreementRefused("unknown_agreement", f"no agreement with the id {agreement_id}")
        introduction = _introduction_parties(connection, row["introduction_id"])
        if account["id"] not in (introduction["from_account"], introduction["to_account"]):
            raise AgreementRefused("not_a_party", "this record is not yours")
        if row["state"] != "agreed":
            raise AgreementRefused("not_agreed_yet",
                                   "milestones start once the terms are agreed, not before")
        if kind == "issue" and len(str(note or "").strip()) < 3:
            raise AgreementRefused("empty_note", "an issue without a word about it is not a record")
        envelope = _evidence(f"agreement.{kind}",
                             {"agreement_id": agreement_id, "by": account["email"], "note": str(note or "")[:400]},
                             {"recorded": True}, authority="self-declared")
        event_id = f"ae-{secrets.token_hex(6)}"
        connection.execute(
            "INSERT INTO agreement_events (id, agreement_id, kind, actor, note, created_at, evidence_id)"
            " VALUES (?,?,?,?,?,?,?)",
            (event_id, agreement_id, kind, account["email"], str(note or "")[:600], _now(),
             envelope["evidence_id"]))
        ids = json.loads(row["evidence_ids"] or "[]") + [envelope["evidence_id"]]
        connection.execute("UPDATE agreements SET evidence_ids = ? WHERE id = ?", (json.dumps(ids), agreement_id))
        _store_envelope(connection, agreement_id, envelope)
    return {"id": event_id, "kind": kind, "evidence_id": envelope["evidence_id"]}


def verify_agreement(row) -> dict:
    """Check the record without trusting this service: rebuild the signed bytes from the stored terms, then
    check both signatures against the public keys of the two accounts, then recompute every evidence id."""
    from arli_engine.core.evidence import Evidence
    from arli_engine.core.signing import verify_signed_bytes

    reasons: list[str] = []
    rebuilt = _agreement_body(row["introduction_id"], row["version"], row["proposed_by_email"],
                              json.loads(row["terms"]))
    body_matches = rebuilt.decode("utf-8") == row["body"]
    if not body_matches:
        reasons.append("the stored bytes are not the bytes the terms give")
    proposer_jwk, proposer_ok = {}, False
    acceptor_jwk, acceptor_ok = {}, None
    with connect() as connection:
        proposer_jwk = json.loads(connection.execute(
            "SELECT public_jwk FROM accounts WHERE id = ?", (row["proposed_by"],)).fetchone()["public_jwk"])
        if row["accepted_by"]:
            acceptor_jwk = json.loads(connection.execute(
                "SELECT public_jwk FROM accounts WHERE id = ?", (row["accepted_by"],)).fetchone()["public_jwk"])
        envelopes = [r["envelope"] for r in connection.execute(
            "SELECT envelope FROM evidence_records WHERE subject = ?", (row["id"],)).fetchall()]
    proposer_ok, why = verify_signed_bytes(proposer_jwk, row["body"].encode("utf-8"),
                                           json.loads(row["signature_proposer"]))
    reasons += [f"proposer: {reason}" for reason in why]
    if row["signature_acceptor"]:
        acceptor_ok, why = verify_signed_bytes(acceptor_jwk, row["body"].encode("utf-8"),
                                               json.loads(row["signature_acceptor"]))
        reasons += [f"acceptor: {reason}" for reason in why]
    elif row["state"] == "agreed":
        acceptor_ok = False
        reasons.append("an agreed record without the second signature")

    evidence_ok = all(Evidence.from_json(json.loads(raw)).verify_id() for raw in envelopes)
    if not evidence_ok:
        reasons.append("an evidence id does not recompute from its envelope")
    return {"body_recomputes": body_matches, "proposer_signature_holds": proposer_ok,
            "acceptor_signature_holds": acceptor_ok, "evidence_ids_recompute": evidence_ok,
            "checked_reasons": reasons}


def agreement_view(account: dict, row) -> dict:
    with connect() as connection:
        events = connection.execute(
            "SELECT id, kind, actor, note, created_at, evidence_id FROM agreement_events"
            " WHERE agreement_id = ? ORDER BY created_at", (row["id"],)).fetchall()
    return {
        "id": row["id"], "introduction_id": row["introduction_id"], "version": row["version"],
        "state": row["state"], "terms": json.loads(row["terms"]),
        "proposed_by": row["proposed_by_email"], "accepted_by": row["accepted_by_email"],
        "created_at": row["created_at"], "accepted_at": row["accepted_at"],
        "yours_to_decide": (row["state"] == "proposed" and account["id"] != row["proposed_by"]),
        "events": [dict(event) for event in events],
        "evidence_ids": json.loads(row["evidence_ids"] or "[]"),
        "verification": verify_agreement(row),
        "signatures": {"proposer": json.loads(row["signature_proposer"]),
                       "acceptor": json.loads(row["signature_acceptor"]) if row["signature_acceptor"] else None},
    }


def agreements_for(account: dict, introduction_id: str) -> dict:
    with connect() as connection:
        introduction = connection.execute(
            "SELECT * FROM introductions WHERE id = ?", (introduction_id,)).fetchone()
        if introduction is None or account["id"] not in (introduction["from_account"], introduction["to_account"]):
            raise AgreementRefused("unknown_introduction", f"no introduction with the id {introduction_id}")
        rows = connection.execute(
            "SELECT * FROM agreements WHERE introduction_id = ? ORDER BY version", (introduction_id,)).fetchall()
    return {"introduction_id": introduction_id,
            "records": [agreement_view(account, row) for row in rows],
            "note": "Terms are signed by both accounts over the same bytes, and the bytes are kept, so anyone "
                    "holding the id can check the signatures instead of taking our word for it."}


def app_summary(account: dict) -> dict:
    """The numbers the screen shows, gathered in one call.

    A pipeline screen that fires six requests to draw its own header feels slow on the one page a person opens
    first, and a number that arrives late is a number nobody trusts. Everything here is a count over rows this
    account can already see, plus the size of the room, which is public by design.
    """
    with connect() as connection:
        agents = connection.execute(
            "SELECT COUNT(*) AS n FROM agents WHERE owner_account = ? AND active = 1",
            (account["id"],)).fetchone()["n"]
        introductions = connection.execute(
            "SELECT state, COUNT(*) AS n FROM introductions WHERE from_account = ? OR to_account = ?"
            " GROUP BY state", (account["id"], account["id"])).fetchall()
        agreements = connection.execute(
            """SELECT a.state, COUNT(*) AS n FROM agreements AS a
               JOIN introductions AS i ON i.id = a.introduction_id
               WHERE i.from_account = ? OR i.to_account = ? GROUP BY a.state""",
            (account["id"], account["id"])).fetchall()
        milestones = connection.execute(
            """SELECT COUNT(*) AS n FROM agreement_events AS e
               JOIN agreements AS a ON a.id = e.agreement_id
               JOIN introductions AS i ON i.id = a.introduction_id
               WHERE i.from_account = ? OR i.to_account = ?""",
            (account["id"], account["id"])).fetchone()["n"]
        registered = connection.execute(
            "SELECT COUNT(*) AS n FROM agents WHERE active = 1").fetchone()["n"]

    with connect() as connection:
        waiting_on_you = connection.execute(
            "SELECT COUNT(*) AS n FROM introductions WHERE to_account = ? AND state = 'requested'",
            (account["id"],)).fetchone()["n"]
    introduction_counts = {row["state"]: row["n"] for row in introductions}
    agreement_counts = {row["state"]: row["n"] for row in agreements}
    return {
        "agents": agents,
        "introductions": {
            # The one number a person acts on: how many answers are owed by them rather than by the other side.
            "waiting_on_you": waiting_on_you,
            **introduction_counts,
        },
        "agreements": agreement_counts,
        "milestones": milestones,
        "room": {"registered_agents": registered, "examples": len(ROSTER) if shows_examples() else 0,
                 "connected_agents": len(remote_agents()), "mode": room_mode()},
    }


def entry_for(agent_id: str) -> dict | None:
    for agent in ROSTER:
        if agent["id"] == agent_id:
            return {**agent, "sample": True}
    for agent in stored_agents(include_features=True):
        if agent["id"] == agent_id:
            return {**agent, "sample": False}
    return None


def update_agent(agent_id: str, token: str, payload: dict) -> dict | None:
    """Change what the owner can change. Only the owner token opens this door."""
    current = owned_agent(agent_id, token)
    if current is None:
        return None
    fields = {}
    for key in ("name", "kind", "city", "headline", "claim_level", "will_not_publish"):
        if key in payload:
            fields[key] = str(payload[key]).strip()
    if "kind" in fields and fields["kind"] not in ("demand", "supply", "person"):
        raise ValueError(f"kind must be demand, supply or person, not {fields['kind']}")
    if "features" in payload:
        features = [str(item).strip() for item in (payload["features"] or []) if str(item).strip()]
        if not features:
            raise ValueError("at least one feature is required: an empty list matches nothing")
        if len(features) > MAX_FEATURES:
            raise ValueError(f"{len(features)} features is above the cap of {MAX_FEATURES}")
        fields["features"] = json.dumps(features)
    if not fields:
        return current

    columns = ", ".join(f"{key} = ?" for key in fields)
    with connect() as connection:
        connection.execute(f"UPDATE agents SET {columns} WHERE id = ? AND owner_token = ?",
                           (*fields.values(), agent_id, token))
    return owned_agent(agent_id, token)


class Handler(BaseHandler):
    server_version = "arli-match/0.3"

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie") or ""
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE:
                return value
        return ""

    def _is_https(self) -> bool:
        """Whether this request arrived over TLS.

        It matters for the cookie: a Secure cookie is not sent back over plain HTTP, so marking it Secure on
        a stand that speaks HTTP makes signing in fail silently. Production is behind nginx, which says so in
        X-Forwarded-Proto; a direct TLS socket says so by itself.
        """
        if str(self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https":
            return True
        return isinstance(getattr(self, "connection", None), ssl.SSLSocket)

    def _send_with_cookie(self, status: int, payload: dict, token: str, clear: bool = False) -> None:
        """A session lives in an HttpOnly cookie: no script on the page can read it, and no page can
        forge it, because the token is random and server-side."""
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        age = 0 if clear else SESSION_DAYS * 24 * 3600
        secure = "; Secure" if self._is_https() else ""
        self.send_header("Set-Cookie",
                         f"{SESSION_COOKIE}={'' if clear else token}; Path=/; HttpOnly; SameSite=Lax"
                         f"{secure}; Max-Age={age}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _serve_page(self, path: Path) -> None:
        """Serve a page without letting it be cached.

        A cached page is a page from an older deployment: the browser keeps the old script, the script calls
        endpoints that have moved, and the person sees a form that does nothing. Cheap to prevent, confusing
        to debug afterwards.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(path.read_bytes())))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(path.read_bytes())

    def _serve_home(self) -> None:
        for name in ("home.html", "index.html"):
            candidate = HERE / name
            if candidate.is_file():
                self._serve_page(candidate)
                return
        self._json(500, {"error": "no page to serve"})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/account":
            page = HERE / "account.html"
            if page.is_file():
                self._serve_page(page)
            else:
                self._json(500, {"error": "no account page"})
            return
        query = dict(
            pair.split("=", 1) for pair in self.path.split("?", 1)[1].split("&") if "=" in pair
        ) if "?" in self.path else {}
        if path in ("/", "/index.html", "/home.html"):
            self._serve_home()
            return
        if path == "/api/room":
            agents = room_view()
            self._json(200, {
                "roster_kind": "sample plus registered",
                "network": "network-eu-design",
                "default_floor_basis_points": DEFAULT_FLOOR,
                "default_mine": DEFAULT_MINE,
                "sample_agents": sum(1 for agent in agents if agent.get("sample")),
                # Not a sample and not a remote: a connected agent is not a registered one, and one word for
                # both numbers is how a room of five appears in one place and of two in another.
                "registered_agents": sum(1 for agent in agents
                                         if not agent.get("sample") and not agent.get("remote")),
                "connected_agents": sum(1 for agent in agents if agent.get("remote")),
                "note": "The starter agents are synthetic and labelled. Registered agents are real "
                        "entries; no feature list is ever returned by this endpoint.",
                "agents": agents,
            })
            return
        if path == "/api/agents":
            token = query.get("token", "")
            with connect() as connection:
                rows = connection.execute(
                    "SELECT id FROM agents WHERE owner_token = ? AND active = 1", (token,)
                ).fetchall() if token else []
            self._json(200, {"agents": [owned_agent(row["id"], token) for row in rows]})
            return
        if path == "/api/remotes":
            self._json(200, {
                "connected_agents": len(remote_agents()),
                "note": "Agents that connected from their own address. Their addresses are not published: "
                        "an address is an invitation to probe.",
                "agents": remote_agents(include_endpoint=False),
            })
            return
        if path == "/api/pull":
            state, agent = pull_token_state(str(query.get("pull_token") or ""))
            if state != "ok":
                self._json(403, {"error": f"pull_token_{state}",
                                 "detail": "The token is issued with a term and is not renewed silently: "
                                           "register again to get a new one." if state == "expired"
                                           else "This token is not a live registration here."})
                return
            call = pull_next(agent["id"])
            self._json(200, call) if call else self._json(204, {})
            return

        if path == "/api/health":
            with connect() as connection:
                registered = connection.execute("SELECT COUNT(*) AS n FROM agents WHERE active = 1").fetchone()["n"]
            self._json(200, {"ok": True, "engine": "arli-discovery", "app": "arli-match",
                             "sample_agents": (len(ROSTER) if shows_examples() else 0), "room_mode": room_mode(), "registered_agents": registered,
                             "connected_agents": len(remote_agents()),
                             "database": str(DB_PATH), "network": "network-eu-design"})
            return
        if path in ("/room", "/room/"):
            # Who is in the room, because a network nobody can see is a network nobody joins.
            self._serve_page(HERE / "room.html")
            return
        if path == "/api/listing":
            self._json(200, room_listing())
            return
        if path in ("/join", "/join/"):
            self._serve_page(HERE / "join.html")
            return
        if path in ("/privacy", "/privacy/"):
            # What the room keeps, written from the store rather than from intentions.
            self._serve_page(HERE / "privacy.html")
            return
        if path in ("/app", "/app/"):
            page = HERE / "app.html"
            if page.is_file():
                self._serve_page(page)
            else:
                self._json(500, {"error": "no app page"})
            return
        if path == "/api/vocabulary":
            given = str(self.headers.get("X-ARLI-Key") or "")
            expected = os.environ.get(INTAKE_KEY_ENV) or os.environ.get("ARLI_INTAKE_KEY") or ""
            if not expected or not secrets.compare_digest(given, expected):
                self._json(403, {"error": "vocabulary_not_published",
                                 "detail": "The vocabulary is the network's licensed data and travels to a "
                                           "holder of this room's key. Ask us for the key and the areas, then "
                                           "declare your areas when you connect."})
                return
            self._json(200, vocabulary())
            return
        if path == "/api/app/summary":
            account = account_by_session(self._cookie_token())
            if account is None:
                self._json(401, {"error": "not_signed_in", "detail": "Sign in first."})
                return
            self._json(200, app_summary(account))
            return
        if path == "/api/app/me":
            account = account_by_session(self._cookie_token())
            self._json(200, {"signed_in": bool(account), "account": account or {}})
            return
        if path == "/api/app/introductions":
            account = account_by_session(self._cookie_token())
            if account is None:
                self._json(401, {"error": "not_signed_in", "detail": "Sign in first."})
                return
            self._json(200, {**introductions_for(account),
                             "note": "A match is a number. Each side agrees separately, and the contact opens "
                                     "only once both have. Every step is recorded with an id you can point at."})
            return
        if path == "/api/app/agents":
            account = account_by_session(self._cookie_token())
            if account is None:
                self._json(401, {"error": "not_signed_in", "detail": "Sign in first."})
                return
            self._json(200, {"agents": app_agents(account["id"])})
            return
        if path == "/api/app/agreements":
            account = account_by_session(self._cookie_token())
            if account is None:
                self._json(401, {"error": "not_signed_in", "detail": "Sign in first."})
                return
            try:
                self._json(200, agreements_for(account, query.get("introduction_id", "")))
            except AgreementRefused as error:
                self._json(409, {"error": error.code, "detail": error.detail})
            return
        if path.startswith("/api/verify/agreement/"):
            agreement_id = path.rsplit("/", 1)[-1]
            with connect() as connection:
                row = connection.execute("SELECT * FROM agreements WHERE id = ?", (agreement_id,)).fetchone()
            if row is None:
                self._json(404, {"error": "unknown_agreement",
                                 "detail": f"no agreement with the id {agreement_id}"})
                return
            self._json(200, {
                "id": row["id"], "state": row["state"], "version": row["version"],
                "terms": json.loads(row["terms"]),
                "proposed_by": row["proposed_by_email"], "accepted_by": row["accepted_by_email"],
                "signed_bytes": row["body"],
                "signatures": {"proposer": json.loads(row["signature_proposer"]),
                               "acceptor": (json.loads(row["signature_acceptor"])
                                            if row["signature_acceptor"] else None)},
                "verification": verify_agreement(row),
                "note": "Anyone holding this id can check that both sides signed these exact bytes. No account "
                        "is needed to check it, and nothing here moves money.",
            })
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path not in ("/api/agents", "/api/scan", "/api/negotiate", "/api/connect", "/api/intake",
                         "/api/join/check",
                         "/api/pull/hello", "/api/pull/register", "/api/pull/answer",
                         "/api/app/register", "/api/app/login", "/api/app/logout",
                         "/api/vocabulary", "/api/app/intake", "/api/app/introductions",
                         "/api/app/agreements") and not (
            path.startswith("/api/app/introductions/") or path.startswith("/api/app/agreements/") or
            path.startswith("/api/agents/") and len(path) > len("/api/agents/")
        ):
            super().do_POST()
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as error:
            self._json(400, {"error": f"bad json: {error}"})
            return

        if path == "/api/agents":
            try:
                agent, token = create_agent(payload)
            except ValueError as error:
                self._json(400, {"error": str(error)})
                return
            except sqlite3.IntegrityError:
                self._json(409, {"error": f"an agent with the id {payload.get('id')} already exists"})
                return
            self._json(201, {
                "agent": agent,
                "token": token,
                "keep_this": "The token is the only key to this agent. It is not stored anywhere else "
                             "in a readable form and is never shown again.",
                "manage_url": f"/account?token={token}",
            })
            return

        if path in ("/api/app/register", "/api/app/login"):
            here = "auth:" + client_ip(self.client_address[0] if self.client_address else "",
                                        str(self.headers.get("X-Forwarded-For") or ""))
            if not attempts_allowed(here):
                self._json(429, {"error": "too_many_attempts",
                                 "detail": "Too many attempts from here. Try again in a few minutes."})
                return
            note_attempt(here)
            if path.endswith("register"):
                try:
                    account, token = register_account(payload.get("email"), payload.get("password"))
                except ValueError as error:
                    self._json(400, {"error": "registration_refused", "detail": str(error)})
                    return
            else:
                account, token = sign_in(payload.get("email"), payload.get("password"))
                if account is None:
                    self._json(401, {"error": "wrong_email_or_password",
                                     "detail": "That email and password do not go together. Nothing is said "
                                               "about which of the two was wrong."})
                    return
            self._send_with_cookie(200, {"signed_in": True, "account": account}, token)
            return

        if path == "/api/app/logout":
            sign_out(self._cookie_token())
            self._send_with_cookie(200, {"signed_in": False}, "", clear=True)
            return

        if path == "/api/app/introductions":
            account = account_by_session(self._cookie_token())
            if account is None:
                self._json(401, {"error": "not_signed_in", "detail": "Sign in first."})
                return
            try:
                result = request_introduction(account, payload.get("peer_id"), str(payload.get("note") or ""))
            except IntroductionRefused as error:
                self._json(409, {"error": error.code, "detail": error.detail})
                return
            self._json(200, result)
            return

        if path.startswith("/api/app/introductions/"):
            parts = path.strip("/").split("/")
            if len(parts) < 4:
                self._json(404, {"error": "not found", "path": path})
                return
            intro_id, action = parts[3], (parts[4] if len(parts) > 4 else "")
            account = account_by_session(self._cookie_token())
            if account is None:
                self._json(401, {"error": "not_signed_in", "detail": "Sign in first."})
                return
            try:
                if action == "decide":
                    decision = str(payload.get("decision") or "").strip().lower()
                    if decision not in ("approve", "decline"):
                        self._json(400, {"error": "decision_required", "detail": "approve or decline"})
                        return
                    result = decide_introduction(account, intro_id, decision == "approve",
                                                 str(payload.get("message") or ""))
                elif action == "messages":
                    result = send_introduction_message(account, intro_id, str(payload.get("body") or ""))
                elif action == "agreement":
                    result = propose_agreement(account, intro_id, payload)
                else:
                    self._json(404, {"error": "not found", "path": path})
                    return
            except IntroductionRefused as error:
                self._json(409, {"error": error.code, "detail": error.detail})
                return
            # A refusal from the terms is a refusal like any other, and an exception that escapes the handler
            # closes the connection with no answer at all, which is the least useful thing a door can do.
            except AgreementRefused as error:
                self._json(409, {"error": error.code, "detail": error.detail})
                return
            self._json(200, result)
            return

        if path.startswith("/api/app/agreements/"):
            parts = path.strip("/").split("/")
            if len(parts) < 5:
                self._json(404, {"error": "not found", "path": path})
                return
            agreement_id, action = parts[3], parts[4]
            account = account_by_session(self._cookie_token())
            if account is None:
                self._json(401, {"error": "not_signed_in", "detail": "Sign in first."})
                return
            try:
                if action == "decide":
                    decision = str(payload.get("decision") or "").strip().lower()
                    if decision not in ("accept", "decline"):
                        self._json(400, {"error": "decision_required", "detail": "accept or decline"})
                        return
                    result = decide_agreement(account, agreement_id, decision == "accept",
                                              str(payload.get("note") or ""))
                elif action == "events":
                    result = record_agreement_event(account, agreement_id,
                                                    str(payload.get("kind") or ""),
                                                    str(payload.get("note") or ""))
                else:
                    self._json(404, {"error": "not found", "path": path})
                    return
            except AgreementRefused as error:
                self._json(409, {"error": error.code, "detail": error.detail})
                return
            self._json(200, result)
            return

        if path == "/api/app/intake":
            account = account_by_session(self._cookie_token())
            if account is None:
                self._json(401, {"error": "not_signed_in",
                                 "detail": "The agent you make from words belongs to an account, so sign in "
                                           "first. That is the whole reason this door is not on the open page."})
                return
            text = str(payload.get("text") or "").strip()
            if len(text) < 3:
                self._json(400, {"error": "write_what_you_need", "detail": "A sentence at least, in your words."})
                return
            result = intake(payload)
            if result.get("created"):
                with connect() as connection:
                    connection.execute("UPDATE agents SET owner_account = ? WHERE id = ?",
                                       (account["id"], result["agent"]["id"]))
                result["manage_url"] = f"https://match.arliwork.com/account?token={result['token']}"
                result["owner"] = account["email"]
            self._json(200, result)
            return

        if path == "/api/intake":
            if not intake_allowed(self.headers.get("X-ARLI-Intake-Key")):
                self._json(403, {
                    "error": "intake_not_open",
                    "detail": "Writing a sentence and getting an agent happens in the app, after signing in: "
                              "an agent made anonymously would have no owner. This deployment creates them "
                              "for the platform only.",
                })
                return
            try:
                self._json(200, intake(payload))
            except ValueError as error:
                self._json(400, {"error": str(error)})
            return

        if path == "/api/join/check":
            here = "join:" + client_ip(self.client_address[0] if self.client_address else "",
                                       str(self.headers.get("X-Forwarded-For") or ""))
            if not attempts_allowed(here, allowed=JOIN_CHECKS_ALLOWED, window=SEARCH_WINDOW_SECONDS):
                self._json(429, {"error": "too_many_checks",
                                 "detail": f"At most {JOIN_CHECKS_ALLOWED} checks from one address every "
                                           f"{SEARCH_WINDOW_SECONDS // 60} minutes."})
                return
            note_attempt(here)
            self._json(200, join_check(str(payload.get("agent") or "")))
            return

        if path == "/api/pull/hello":
            try:
                self._json(200, pull_hello(payload.get("card") or {}))
            except ConnectRefused as error:
                self._json(400, {"error": error.code, "detail": error.detail})
            return

        if path == "/api/pull/register":
            try:
                self._json(200, pull_register(payload))
            except ConnectRefused as error:
                self._json(400, {"error": error.code, "detail": error.detail})
            return

        if path == "/api/pull/answer":
            state, agent = pull_token_state(str(payload.get("pull_token") or ""))
            if state != "ok":
                self._json(403, {"error": f"pull_token_{state}",
                                 "detail": "The token is issued with a term and is not renewed silently: "
                                           "register again to get a new one." if state == "expired"
                                           else "This token is not a live registration here."})
                return
            accepted = pull_answer(agent["id"], str(payload.get("call_id") or ""),
                                   payload.get("result"), str(payload.get("error") or ""))
            self._json(200 if accepted else 409, {"accepted": accepted})
            return

        if path == "/api/connect":
            try:
                self._json(200, connect_agent(payload, room=str(self.headers.get("Host") or "")))
            except ConnectRefused as error:
                self._json(400, {"error": error.code, "detail": error.detail})
            return

        if path.startswith("/api/agents/") and len(path) > len("/api/agents/"):
            agent_id = path.rsplit("/", 1)[-1]
            token = str(payload.get("token") or "")
            try:
                updated = update_agent(agent_id, token, payload)
            except ValueError as error:
                self._json(400, {"error": str(error)})
                return
            if updated is None:
                self._json(404, {"error": "no such agent for this token"})
                return
            self._json(200, {"agent": updated})
            return

        if path == "/api/negotiate":
            agent_id = str(payload.get("agent_id") or "")
            entry = entry_for(agent_id)
            if entry is None:
                self._json(404, {"error": f"no such agent in the room: {agent_id}"})
                return
            mine = features_from_payload(payload)
            floor = int(payload.get("floor_basis_points") or DEFAULT_FLOOR)
            my_kind = str(payload.get("my_kind") or "demand")
            try:
                result = run_exchange(mine, list(entry["features"]), floor, seeker_name="you",
                                      holder_name=entry["id"], seeker_kind=my_kind,
                                      holder_kind=entry["kind"])
                result["agent"] = {key: entry[key] for key in
                                   ("id", "name", "kind", "city", "headline", "claim_level",
                                    "will_not_publish")}
                self._json(200, result)
            except Exception as error:  # noqa: BLE001
                self._json(500, {"error": f"{type(error).__name__}: {error}"})
            return

        mine = features_from_payload(payload)
        floor = int(payload.get("floor_basis_points") or DEFAULT_FLOOR)
        my_kind = str(payload.get("my_kind") or "demand")
        if my_kind not in ("demand", "supply", "person"):
            self._json(400, {"error": f"unknown intent type: {my_kind}"})
            return
        if not mine:
            self._json(400, {"error": "your side needs at least one feature: an empty set matches nothing"})
            return
        if not mine:
            # The same rule as registration and as a single pair: an empty set matches nothing, so
            # answering with a full scan would be work done for a question nobody asked.
            self._json(400, {"error": "your side needs at least one feature: an empty set matches nothing"})
            return
        if floor < 0 or floor > 10000:
            # Both ends, not just the top: a negative floor is not stricter, it is meaningless, and
            # it used to be accepted without a word.
            self._json(400, {"error": f"floor_basis_points must be between 0 and 10000, not {floor}"})
            return
        if len(mine) > MAX_FEATURES:
            # The same cap registration enforces. Accepting more here would mean the search silently
            # disagrees with the protocol it is built on.
            self._json(400, {"error": f"{len(mine)} features is above the cap of {MAX_FEATURES} per exchange"})
            return
        here = "scan:" + client_ip(self.client_address[0] if self.client_address else "",
                                   str(self.headers.get("X-Forwarded-For") or ""))
        if not attempts_allowed(here, allowed=SEARCH_ATTEMPTS_ALLOWED, window=SEARCH_WINDOW_SECONDS):
            # One search is one closed exchange per agent sharing a bucket, which is our machine working for a
            # stranger. Without a limit per address, one caller can spend the whole room's capacity.
            self._json(429, {"error": "too_many_searches",
                             "detail": f"At most {SEARCH_ATTEMPTS_ALLOWED} searches from one address every "
                                       f"{SEARCH_WINDOW_SECONDS // 60} minutes."})
            return
        note_attempt(here)
        try:
            result = scan_room(mine, floor, my_kind)
            # An agent is not a pair with its own owner, and a search that shows a person their own agent at
            # full overlap is noise pretending to be a find.
            account = account_by_session(self._cookie_token())
            if account is not None:
                with connect() as connection:
                    mine_ids = {row["id"] for row in connection.execute(
                        "SELECT id FROM agents WHERE owner_account = ?", (account["id"],)).fetchall()}
                if mine_ids:
                    before = len(result["rows"])
                    result["rows"] = [row for row in result["rows"] if row.get("id") not in mine_ids]
                    result["your_own_agents_excluded"] = before - len(result["rows"])
                    result["agents_checked"] = len(result["rows"])
                    result["above_floor"] = sum(1 for row in result["rows"] if row.get("passed_floor"))
            self._json(200, result)
        except Exception as error:  # noqa: BLE001
            self._json(500, {"error": f"{type(error).__name__}: {error}"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/api/app/account" or path.startswith("/api/app/agents/"):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                payload = {}
            account = account_by_session(self._cookie_token())
            if account is None:
                self._json(401, {"error": "not_signed_in", "detail": "Sign in first."})
                return
            if path == "/api/app/account":
                if not _verify_password(str(payload.get("password") or ""), _stored_hash(account["id"])):
                    self._json(403, {"error": "password_required",
                                     "detail": "Closing an account needs the password that opens it."})
                    return
                closed = close_account(account["id"])
                self._send_with_cookie(200, {"closed": True, **closed}, "", clear=True)
                return
            agent_id = path.rsplit("/", 1)[-1]
            with connect() as connection:
                cursor = connection.execute(
                    "UPDATE agents SET active = 0 WHERE id = ? AND owner_account = ?",
                    (agent_id, account["id"]))
            if cursor.rowcount == 0:
                self._json(404, {"error": "no such agent for this account"})
                return
            self._json(200, {"id": agent_id, "active": False})
            return
        if not path.startswith("/api/agents/"):
            self._json(404, {"error": "not found", "path": path})
            return
        agent_id = path.rsplit("/", 1)[-1]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            payload = {}
        token = str(payload.get("token") or "")
        with connect() as connection:
            cursor = connection.execute(
                "UPDATE agents SET active = 0 WHERE id = ? AND owner_token = ?", (agent_id, token)
            )
        if cursor.rowcount == 0:
            self._json(404, {"error": "no such agent for this token"})
            return
        self._json(200, {"id": agent_id, "active": False})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8141)
    args = parser.parse_args()
    init()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    registered = len(stored_agents())
    print(f"arli match service on http://127.0.0.1:{args.port} "
          f"({len(ROSTER)} starter agents, {registered} registered, store at {DB_PATH})")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
