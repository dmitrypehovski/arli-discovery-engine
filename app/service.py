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

from app.server import DEFAULT_FLOOR, DEFAULT_MINE, ROSTER, Handler as BaseHandler, public_view  # noqa: E402
from demo.exchange import run_exchange  # noqa: E402

HERE = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ARLI_MATCH_DB", "/var/lib/arli-match/match.sqlite3"))
MAX_FEATURES = 64


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init() -> None:
    with connect() as connection:
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


def room_view() -> list[dict]:
    """Everyone in the room: the starter agents plus everyone who registered. Never a feature."""
    samples = [{**public_view(agent), "stored": False, "sample": True} for agent in ROSTER]
    stored = [{key: value for key, value in agent.items() if key != "features"}
              for agent in stored_agents(include_features=True)]
    return samples + stored


def create_agent(payload: dict) -> tuple[dict, str]:
    name = str(payload.get("name") or "").strip()
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


def scan_room(my_features: list[str], floor: int, my_kind: str) -> dict:
    """One exchange per agent in the room, samples and registered agents alike."""
    entries = []
    for agent in ROSTER:
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
    rows.sort(key=lambda row: (row.get("overlap_basis_points") or -1), reverse=True)
    return {
        "roster_kind": "sample plus registered",
        "floor_basis_points": floor,
        "your_kind": my_kind,
        "agents_checked": len(rows),
        "registered_agents": sum(1 for row in rows if not row.get("sample")),
        "above_floor": sum(1 for row in rows if row.get("passed_floor")),
        "not_comparable": sum(1 for row in rows if not row.get("opened")),
        "wires_clean": all(row.get("wires_clean", False) for row in rows),
        "your_feature_count": len(my_features),
        "rows": rows,
    }


def entry_for(agent_id: str) -> dict | None:
    for agent in ROSTER:
        if agent["id"] == agent_id:
            return {**agent, "sample": True}
    for agent in stored_agents(include_features=True):
        if agent["id"] == agent_id:
            return {**agent, "sample": False}
    return None


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
        if path == "/api/health":
            with connect() as connection:
                registered = connection.execute("SELECT COUNT(*) AS n FROM agents WHERE active = 1").fetchone()["n"]
            self._json(200, {"ok": True, "engine": "arli-discovery", "app": "arli-match",
                             "sample_agents": len(ROSTER), "registered_agents": registered,
                             "database": str(DB_PATH), "network": "network-eu-design"})
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path not in ("/api/agents", "/api/scan", "/api/negotiate"):
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

        if path == "/api/negotiate":
            agent_id = str(payload.get("agent_id") or "")
            entry = entry_for(agent_id)
            if entry is None:
                self._json(404, {"error": f"no such agent in the room: {agent_id}"})
                return
            mine = [str(item).strip() for item in (payload.get("my_features") or DEFAULT_MINE)
                    if str(item).strip()]
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

        mine = [str(item).strip() for item in (payload.get("my_features") or DEFAULT_MINE) if str(item).strip()]
        floor = int(payload.get("floor_basis_points") or DEFAULT_FLOOR)
        my_kind = str(payload.get("my_kind") or "demand")
        if my_kind not in ("demand", "supply", "person"):
            self._json(400, {"error": f"unknown intent type: {my_kind}"})
            return
        if not mine:
            self._json(400, {"error": "your side needs at least one feature: an empty set matches nothing"})
            return
        if floor > 10000:
            self._json(400, {"error": f"floor_basis_points={floor} is above the whole set"})
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
