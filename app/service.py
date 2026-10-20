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
import sys
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
               active INTEGER NOT NULL DEFAULT 1
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
    for column, definition in (("failures", "INTEGER NOT NULL DEFAULT 0"),
                               ("last_checked", "TEXT NOT NULL DEFAULT ''"),
                               ("unreachable_reason", "TEXT NOT NULL DEFAULT ''")):
        try:
            connection.execute(f"ALTER TABLE remotes ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass
    # Which account made an agent, so a signed-in person can find their own again.
    try:
        connection.execute("ALTER TABLE agents ADD COLUMN owner_account TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    return connection


def init() -> None:
    with connect():
        pass


def connect_agent(payload: dict) -> dict:
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
            answer = client.call("arli/identity/sign", {"nonce": nonce, "purpose": "connect"})
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
    with connect() as connection:
        connection.execute(
            """INSERT OR REPLACE INTO remotes
               (id, name, kind, city, headline, endpoint, kid, jwk, connected_at, active)
               VALUES (?,?,?,?,?,?,?,?,?,1)""",
            (agent_id, verified["name"], kind, str(payload.get("city") or "").strip(),
             str(payload.get("headline") or "").strip(), endpoint, verified["kid"],
             json.dumps(verified["jwk"]), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
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
        "note": "Connected from its own address. The card verified, the identity method answered, and "
                "the address signed a nonce this service chose, so the key the card names is reachable "
                "there. A proxy that forwards that signing passes too: it then holds a live path to "
                "the agent, which is the trust a relay has.",
    }


def remote_agents(include_endpoint: bool = False) -> list[dict]:
    with connect() as connection:
        rows = connection.execute("SELECT * FROM remotes WHERE active = 1 ORDER BY connected_at").fetchall()
    agents = []
    for row in rows:
        agent = {"id": row["id"], "name": row["name"], "kind": row["kind"], "city": row["city"],
                 "headline": row["headline"], "claim_level": "declared",
                 "will_not_publish": "whatever its owner keeps at home",
                 "feature_count": None, "remote": True, "connected_at": row["connected_at"]}
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
    return [{"id": row["id"], "name": row["name"], "kind": row["kind"], "city": row["city"],
             "headline": row["headline"], "endpoint": row["endpoint"], "active": row["active"],
             "failures": row["failures"]} for row in rows]


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
    name = str(payload.get("name") or "").strip()
    if len(name) > MAX_NAME_CHARS:
        raise ValueError(f"a name of {MAX_NAME_CHARS} characters at most, not {len(name)}")
    kind = str(payload.get("kind") or "demand").strip()
    features = [str(item).strip() for item in (payload.get("features") or []) if str(item).strip()]
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
                str(payload.get("headline") or "").strip(),
                str(payload.get("claim_level") or "declared").strip(),
                str(payload.get("will_not_publish") or "").strip(),
                json.dumps(features), token, created,
            ),
        )
    return {"id": agent_id, "name": name, "kind": kind, "feature_count": len(features),
            "created_at": created}, token


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

    rows = []
    for entry in entries:
        try:
            result = run_exchange(my_features, entry["features"], floor, seeker_name="you",
                                  holder_name=entry["id"], seeker_kind=my_kind,
                                  holder_kind=entry["kind"])
        except Exception as error:  # noqa: BLE001
            rows.append({"id": entry["id"], "name": entry["name"], "error": f"{type(error).__name__}: {error}"})
            continue
        rows.append({
            "id": entry["id"], "name": entry["name"], "kind": entry["kind"], "city": entry["city"],
            "headline": entry["headline"], "claim_level": entry["claim_level"],
            "will_not_publish": entry["will_not_publish"], "feature_count": entry["feature_count"],
            "sample": entry["sample"],
            "opened": result.get("opened", False), "rejection": result.get("rejection"),
            "overlap_basis_points": result["overlap_basis_points"],
            "overlap_ratio": result["overlap_ratio"],
            "matched_features": result["matched_features"],
            "passed_floor": result["passed_floor"], "agreed": result["agreed"],
            "wires_clean": result["leak_check"]["clean"],
            "evidence_ids": {name: envelope["evidence_id"]
                             for name, envelope in result["evidence"].items()},
        })
    for remote in remotes_to_check():
        try:
            result = run_remote_exchange(
                my_features, remote["endpoint"], floor, remote_id=remote["name"],
                remote_kind=remote["kind"], my_kind=my_kind,
            )
        except Exception as error:  # noqa: BLE001
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


def attempts_allowed(bucket: str) -> bool:
    """A blunt limit on registering and signing in: enough for a person, too little for a script."""
    now = time.time()
    recent = [moment for moment in _attempts.get(bucket, []) if now - moment < ATTEMPT_WINDOW_SECONDS]
    _attempts[bucket] = recent
    return len(recent) < ATTEMPTS_ALLOWED


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
            """SELECT a.id AS id, a.email AS email
               FROM sessions AS s JOIN accounts AS a ON a.id = s.account_id
               WHERE s.token = ? AND s.expires_at > ?""",
            (token, _now()),
        ).fetchone()
    return dict(row) if row else None


def register_account(email, password) -> tuple[dict, str]:
    email = _clean_email(email)
    password = str(password or "")
    if len(password) < MIN_PASSWORD_CHARS:
        raise ValueError(f"a password of at least {MIN_PASSWORD_CHARS} characters")
    account_id = f"u-{secrets.token_hex(6)}"
    with connect() as connection:
        if connection.execute("SELECT id FROM accounts WHERE email = ?", (email,)).fetchone():
            raise ValueError("an account with this email already exists: sign in instead")
        connection.execute("INSERT INTO accounts (id, email, password_hash, created_at) VALUES (?,?,?,?)",
                           (account_id, email, _hash_password(password), _now()))
        token = _open_session(connection, account_id)
    return {"id": account_id, "email": email}, token


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
        if not peer_owner:
            raise IntroductionRefused(
                "peer_not_hosted_here",
                "this agent is not hosted on this service, so an introduction cannot be delivered here. A "
                "connected agent would have to answer arli/opportunity/decide at its own address, and that "
                "step is not built yet.",
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

    def _serve_home(self) -> None:
        for name in ("home.html", "index.html"):
            candidate = HERE / name
            if candidate.is_file():
                self._send(200, candidate.read_bytes(), "text/html; charset=utf-8")
                return
        self._json(500, {"error": "no page to serve"})

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/account":
            page = HERE / "account.html"
            if page.is_file():
                self._send(200, page.read_bytes(), "text/html; charset=utf-8")
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
                "registered_agents": sum(1 for agent in agents if not agent.get("sample")),
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
        if path == "/api/health":
            with connect() as connection:
                registered = connection.execute("SELECT COUNT(*) AS n FROM agents WHERE active = 1").fetchone()["n"]
            self._json(200, {"ok": True, "engine": "arli-discovery", "app": "arli-match",
                             "sample_agents": (len(ROSTER) if shows_examples() else 0), "room_mode": room_mode(), "registered_agents": registered,
                             "connected_agents": len(remote_agents()),
                             "database": str(DB_PATH), "network": "network-eu-design"})
            return
        if path in ("/app", "/app/"):
            page = HERE / "app.html"
            if page.is_file():
                self._send(200, page.read_bytes(), "text/html; charset=utf-8")
            else:
                self._json(500, {"error": "no app page"})
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
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path not in ("/api/agents", "/api/scan", "/api/negotiate", "/api/connect", "/api/intake",
                         "/api/app/register", "/api/app/login", "/api/app/logout",
                         "/api/app/intake", "/api/app/introductions") and not (
            path.startswith("/api/app/introductions/") or
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
            here = f"auth:{self.client_address[0] if self.client_address else 'unknown'}"
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
                else:
                    self._json(404, {"error": "not found", "path": path})
                    return
            except IntroductionRefused as error:
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

        if path == "/api/connect":
            try:
                self._json(200, connect_agent(payload))
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
