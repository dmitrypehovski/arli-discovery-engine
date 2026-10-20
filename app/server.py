"""ARLI Match: match against a room of agents without publishing your list.

Why this is not the demonstration. The demonstration shows one pair and its nine calls. This is the
thing a person would actually use: a room of counterparties, one scan, and a ranked board of who
overlaps with you above the floor you set. The list you scan with never leaves your side, and the
room's lists never leave theirs; each row is produced by a full exchange with a live agent.

The room here is synthetic and says so, in the payload and on the page.

Run: .venv/bin/python app/server.py --port 8141
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from demo.exchange import NETWORK, run_exchange  # noqa: E402

HERE = Path(__file__).resolve().parent
DEFAULT_FLOOR = 3000
ROSTER_KIND = "synthetic"

# Each entry is a counterparty agent raised by the engine when a scan reaches it. The features are
# what that agent holds privately; the page never shows them, and neither does this server.
ROSTER = [
    {
        "id": "infra-co-sf", "name": "Northline Infrastructure", "kind": "supply", "city": "sf",
        "headline": "SOC 2 infrastructure, three teams free, wants design help",
        "claim_level": "third_party_verified",
        "will_not_publish": "client list, capacity by quarter, prices",
        "features": ["offer:infra", "city:sf", "compliance:soc2", "capacity:3-teams",
                     "need:brand-design", "industry:fintech"],
    },
    {
        "id": "studio-north", "name": "Studio Nord", "kind": "supply", "city": "berlin",
        "headline": "Brand and packaging studio, eight people, retail and consumer",
        "claim_level": "declared",
        "will_not_publish": "client roster, day rates, pipeline",
        "features": ["skill:brand-design", "city:berlin", "industry:retail",
                     "capacity:2-teams", "need:infra", "compliance:gdpr"],
    },
    {
        "id": "fintech-labs", "name": "Fintech Labs", "kind": "demand", "city": "sf",
        "headline": "Series B payments company buying infrastructure and design",
        "claim_level": "topic_owner_verified",
        "will_not_publish": "roadmap, vendor shortlist, budget ceiling",
        "features": ["need:infra", "need:brand-design", "city:sf", "industry:fintech",
                     "stage:series-b", "compliance:soc2"],
    },
    {
        "id": "retail-group", "name": "Kaufhof Digital", "kind": "demand", "city": "berlin",
        "headline": "Retail group rebuilding its brand, GDPR-bound",
        "claim_level": "declared",
        "will_not_publish": "store performance, supplier contracts, budget",
        "features": ["need:brand-design", "city:berlin", "industry:retail",
                     "stage:enterprise", "compliance:gdpr", "budget:under-50k"],
    },
    {
        "id": "ml-lab", "name": "Alpine Model Lab", "kind": "supply", "city": "zurich",
        "headline": "Model tuning and evaluation, one team free",
        "claim_level": "declared",
        "will_not_publish": "client models, benchmark results, rates",
        "features": ["skill:model-tuning", "city:zurich", "capacity:1-team", "need:infra",
                     "industry:health", "compliance:gdpr"],
    },
    {
        "id": "grant-office", "name": "Horizon Grant Office", "kind": "demand", "city": "brussels",
        "headline": "Public programme pairing suppliers with funded consortia",
        "claim_level": "topic_owner_verified",
        "will_not_publish": "applicant names, scoring, remaining envelope",
        "features": ["grant:eu-horizon", "need:infra", "city:brussels", "compliance:gdpr",
                     "industry:health", "stage:consortium"],
    },
    {
        "id": "logistics-eu", "name": "Maas Logistics", "kind": "supply", "city": "rotterdam",
        "headline": "EU fulfilment network, wants brand work for a spin-out",
        "claim_level": "declared",
        "will_not_publish": "volumes, lanes, contract terms",
        "features": ["offer:logistics", "city:rotterdam", "compliance:gdpr", "need:brand-design",
                     "industry:retail", "capacity:4-teams"],
    },
    {
        "id": "legal-guild", "name": "Guild Contracts", "kind": "supply", "city": "london",
        "headline": "Contract review for payments and health, GDPR experience",
        "claim_level": "third_party_verified",
        "will_not_publish": "matters handled, hourly rates, client names",
        "features": ["skill:contract-review", "city:london", "compliance:gdpr",
                     "industry:fintech", "industry:health", "need:infra"],
    },
]

BY_ID = {agent["id"]: agent for agent in ROSTER}

DEFAULT_MINE = ["need:infra", "need:brand-design", "city:sf", "industry:fintech",
                "stage:series-b", "budget:under-250k"]


def public_view(agent: dict) -> dict:
    """What the room publishes about itself: identity and claims, never the feature list."""
    return {
        "id": agent["id"], "name": agent["name"], "kind": agent["kind"], "city": agent["city"],
        "headline": agent["headline"], "claim_level": agent["claim_level"],
        "will_not_publish": agent["will_not_publish"],
        "feature_count": len(agent["features"]),
    }


def scan(my_features: list[str], floor: int, my_kind: str = "demand") -> dict:
    rows = []
    for agent in ROSTER:
        try:
            result = run_exchange(
                my_features, list(agent["features"]), floor,
                seeker_name="you", holder_name=agent["id"],
                seeker_kind=my_kind, holder_kind=agent["kind"],
            )
        except Exception as error:  # noqa: BLE001
            rows.append({**public_view(agent), "error": f"{type(error).__name__}: {error}"})
            continue
        rows.append({
            **public_view(agent),
            "opened": result.get("opened", False),
            "rejection": result.get("rejection"),
            "overlap_basis_points": result["overlap_basis_points"],
            "overlap_ratio": result["overlap_ratio"],
            "matched_features": result["matched_features"],
            "passed_floor": result["passed_floor"],
            "agreed": result["agreed"],
            "wires_clean": result["leak_check"]["clean"],
            "evidence_ids": {name: envelope["evidence_id"]
                             for name, envelope in result["evidence"].items()},
        })
    rows.sort(key=lambda row: (row.get("overlap_basis_points") or -1), reverse=True)
    return {
        "roster_kind": ROSTER_KIND,
        "floor_basis_points": floor,
        "agents_checked": len(rows),
        "above_floor": sum(1 for row in rows if row.get("passed_floor")),
        "not_comparable": sum(1 for row in rows if not row.get("opened")),
        "wires_clean": all(row.get("wires_clean", False) for row in rows),
        "your_kind": my_kind,
        "your_feature_count": len(my_features),
        "rows": rows,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "arli-match/0.1"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/roster":
            self._json(200, {
                "roster_kind": ROSTER_KIND,
                "network": NETWORK,
                "default_floor_basis_points": DEFAULT_FLOOR,
                "default_mine": DEFAULT_MINE,
                "default_mine_kind": "demand",
                "note": "The room is synthetic: these are engine agents raised by this server, not real companies.",
                "agents": [public_view(agent) for agent in ROSTER],
            })
        elif path == "/api/health":
            self._json(200, {"ok": True, "engine": "arli-discovery", "app": "arli-match",
                             "roster": len(ROSTER), "network": NETWORK})
        else:
            self._json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except json.JSONDecodeError as error:
            self._json(400, {"error": f"bad json: {error}"})
            return

        if path == "/api/board":
            mine = [str(item) for item in (payload.get("my_features") or DEFAULT_MINE)]
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
                self._json(200, scan(mine, floor, my_kind))
            except Exception as error:  # noqa: BLE001
                self._json(500, {"error": f"{type(error).__name__}: {error}"})
            return

        if path == "/api/negotiate":
            agent_id = str(payload.get("agent_id") or "")
            entry = BY_ID.get(agent_id)
            if entry is None:
                self._json(404, {"error": f"no such agent in the room: {agent_id}"})
                return
            mine = [str(item) for item in (payload.get("my_features") or DEFAULT_MINE)]
            floor = int(payload.get("floor_basis_points") or DEFAULT_FLOOR)
            try:
                my_kind = str(payload.get("my_kind") or "demand")
                result = run_exchange(mine, list(entry["features"]), floor,
                                      seeker_name="you", holder_name=entry["id"],
                                      seeker_kind=my_kind, holder_kind=entry["kind"])
                result["agent"] = public_view(entry)
                self._json(200, result)
            except Exception as error:  # noqa: BLE001
                self._json(500, {"error": f"{type(error).__name__}: {error}"})
            return

        self._json(404, {"error": "not found", "path": path})

    def log_message(self, *args, **kwargs) -> None:  # console stays quiet
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8141)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"arli match on http://127.0.0.1:{args.port} with {len(ROSTER)} agents in the room")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
