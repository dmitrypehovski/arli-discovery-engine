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
import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timezone
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
    for column, definition in (("failures", "INTEGER NOT NULL DEFAULT 0"),
                               ("last_checked", "TEXT NOT NULL DEFAULT ''"),
                               ("unreachable_reason", "TEXT NOT NULL DEFAULT ''")):
        try:
            connection.execute(f"ALTER TABLE remotes ADD COLUMN {column} {definition}")
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
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path not in ("/api/agents", "/api/scan", "/api/negotiate", "/api/connect", "/api/intake") and not (
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
            self._json(200, scan_room(mine, floor, my_kind))
        except Exception as error:  # noqa: BLE001
            self._json(500, {"error": f"{type(error).__name__}: {error}"})

    def do_DELETE(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
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
