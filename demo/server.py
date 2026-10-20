"""The live demonstration server.

Why this exists. A page can describe a matching engine; only a running one shows what actually
travels. This server raises two REAL agents from the engine package on a private port, drives them
through one complete exchange with the same client the conformance suite uses, and returns the
transcript: every request, every response, and the check that no plain feature ever left either
side. It is the engine doing the work, not a mock of it.

Run: .venv/bin/python demo/server.py --port 8131
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arli_engine.client import AgentClient  # noqa: E402
from arli_engine.core.signing import verify_card_self  # noqa: E402
from arli_engine.serve import build_pair, port_of  # noqa: E402

HERE = Path(__file__).resolve().parent
NETWORK = "network-eu-design"
FLOOR_BASIS_POINTS = 4000

SCENARIO = {
    "seeker": {
        "label": "Design studio looking for an infrastructure partner",
        "features": ["skill:brand-design", "city:sf", "need:infra", "budget:under-50k", "stage:seed"],
    },
    "holder": {
        "label": "Infrastructure provider looking for design work",
        "features": ["offer:infra", "skill:brand-design", "city:sf", "capacity:3-teams", "compliance:soc2"],
    },
}


def _clip(value, limit: int = 132):
    """Long blinded numbers are cut for display only; the count is always reported."""
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Transcript:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.wire: list[dict] = []

    def step(self, name: str, actor: str, params: dict, response: dict, note: str) -> None:
        self.steps.append({
            "name": name,
            "actor": actor,
            "note": note,
            "request": _clip(params),
            "response": _clip(response),
        })
        self.wire.append({"name": name, "payload": json.dumps(params, ensure_ascii=False)})


def run_exchange(seeker_features: list[str], holder_features: list[str], floor: int) -> dict:
    states, servers = build_pair(0, 0)
    transcript = Transcript()
    try:
        states[0].features = type(states[0].features)(seeker_features)
        states[1].features = type(states[1].features)(holder_features)
        port_a, port_b = port_of(servers[0]), port_of(servers[1])
        # Seats are the agent names the servers answer to. Each turn goes to the address of the
        # side that takes it: an agent may only hold its own seat, and that refusal is a feature.
        seat_a, seat_b = states[0].name, states[1].name
        with AgentClient(f"http://127.0.0.1:{port_a}") as seeker, \
                AgentClient(f"http://127.0.0.1:{port_b}") as holder:

            held_cards = [
                {
                    "who": "seeker",
                    "card": seeker.get_card(),
                    "verified": verify_card_self(seeker.get_card()),
                },
                {
                    "who": "holder",
                    "card": holder.get_card(),
                    "verified": verify_card_self(holder.get_card()),
                },
            ]

            session = f"demo-{threading.get_ident()}-{len(transcript.steps)}"
            open_params = {
                "negotiation_id": session,
                "peers": [seat_a, seat_b],
                "network": NETWORK,
                "intents": [
                    {
                        "intent_id": "seeker-1", "owner": seat_a, "type": "demand",
                        "network": NETWORK, "provenance": "human", "active": True,
                        "claim_level": "declared",
                    },
                    {
                        "intent_id": "holder-1", "owner": seat_b, "type": "supply",
                        "network": NETWORK, "provenance": "human", "active": True,
                        "claim_level": "declared",
                    },
                ],
            }
            opened = seeker.call("arli/negotiation/open", open_params)
            holder.call("arli/negotiation/open", open_params)
            transcript.step(
                "Open the pair", "seeker",
                {"negotiation_id": session, "network": NETWORK, "intents": "demand against supply"},
                {"opened": opened["opened"]},
                "The pair opens only because the two intents face each other: a demand and a supply "
                "in the same network, owned by different sides.",
            )

            propose = seeker.call("arli/negotiation/turn", {
                "negotiation_id": session, "seat": seat_a, "action": "propose",
                "message": {"scope": "matching only, no data exchange"},
            })
            transcript.step(
                "First turn", "seeker", {"seat": seat_a, "action": "propose"},
                {"turn": propose["turn"]["action"], "outcome": propose.get("outcome")},
                "The grammar is closed: the first turn can only be a proposal or a decline.",
            )
            accept = holder.call("arli/negotiation/turn", {
                "negotiation_id": session, "seat": seat_b, "action": "accept",
                "message": {"agreed": True},
            })
            transcript.step(
                "Second turn", "holder", {"seat": seat_b, "action": "accept"},
                {"turn": accept["turn"]["action"], "outcome": accept.get("outcome")},
                "Seats must alternate, and accept is legal only right after a proposal.",
            )
            decision_a = seeker.call("arli/opportunity/decide", {
                "negotiation_id": session, "actor": seat_a, "approve": True,
            })
            decision_b = holder.call("arli/opportunity/decide", {
                "negotiation_id": session, "actor": seat_b, "approve": True,
            })
            transcript.step(
                "Human decision", "both", {"actor": "each side, on its own", "approve": True},
                {"consent": decision_b.get("human_consent") or decision_a.get("human_consent"),
                 "status": decision_b.get("status") or decision_a.get("status")},
                "An agent agreeing never opens a relationship. Each side's person decides, as a "
                "separate act, and both answers are needed.",
            )

            keys = holder.call("arli/psi/keys", {"session_id": session})
            transcript.step(
                "Publish the pair key", "holder",
                {"session_id": session},
                {"scheme": keys["scheme"], "feature_cap": keys["feature_cap"], "modulus_bits": len(keys["n"]) * 4},
                "Only the public part of the key leaves the holder. The private part never moves.",
            )
            commit = seeker.call("arli/psi/commit", {
                "session_id": session, "peer_name": seat_b, "n": keys["n"], "e": keys["e"],
            })
            transcript.step(
                "Blind and send", "seeker",
                {"session_id": session, "peer_name": seat_b, "blinded_count": len(commit["blinded"])},
                {"count": len(commit["blinded"]), "note": commit.get("note")},
                "The seeker sends blinded values. On the wire there are no features, only numbers that "
                "cannot be read back without the private key.",
            )
            signed = holder.call("arli/psi/sign", {"session_id": session, "blinded": commit["blinded"]})
            transcript.step(
                "Sign blind", "holder",
                {"session_id": session, "blinded_count": len(commit["blinded"])},
                {"signed_count": len(signed["signed"]), "holder_tokens_count": len(signed["holder_tokens"])},
                "The holder signs what it cannot read, and adds signatures over its own features.",
            )
            finish = seeker.call("arli/psi/finish", {
                "session_id": session, "n": keys["n"], "signed": signed["signed"],
                "holder_tokens": signed["holder_tokens"], "floor_basis_points": floor,
            })
            transcript.step(
                "Unblind and compare", "seeker",
                {"session_id": session, "floor_basis_points": floor},
                {
                    "matched_features": finish["matched_features"],
                    "overlap_ratio": finish.get("overlap_ratio"),
                    "overlap_basis_points": finish["overlap_basis_points"],
                },
                "The seeker learns only the intersection among its own features, and only above the "
                "floor it declared itself.",
            )
            record = holder.call("arli/psi/record", {
                "session_id": session, "matched": finish["matched_features"],
                "floor_basis_points": floor,
            })
            transcript.step(
                "Record on the other side", "holder",
                {"session_id": session, "matched_count": len(record["matched_features"])},
                {"matched_features": record["matched_features"], "overlap_basis_points": record["overlap_basis_points"]},
                "Both sides hold a signed envelope over the same intersection.",
            )

            evidence = {
                "turn": propose.get("evidence"),
                "finish": finish.get("evidence"),
                "record": record.get("evidence"),
            }

        leaked = []
        for item in transcript.wire:
            for side, features in ((seat_a, seeker_features), (seat_b, holder_features)):
                for feature in features:
                    if re.search(re.escape(feature), item["payload"], re.IGNORECASE):
                        leaked.append({"step": item["name"], "side": side, "feature": feature})

        return {
            "cards": [
                {"who": item["who"], "verified": item["verified"][0],
                 "reasons": item["verified"][1], "name": item["card"].get("name")}
                for item in held_cards
            ],
            "steps": transcript.steps,
            "matched_features": finish["matched_features"],
            "overlap_basis_points": finish["overlap_basis_points"],
            "overlap_ratio": finish.get("overlap_ratio"),
            "floor_basis_points": floor,
            "passed_floor": int(finish["overlap_basis_points"]) >= floor,
            "agreed": accept.get("outcome") == "agreed",
            "leaked_features": leaked,
            "leak_check": {
                "checked_steps": len(transcript.wire),
                "features_on_each_side": [len(seeker_features), len(holder_features)],
                "clean": not leaked,
            },
            "evidence": {
                name: {
                    "evidence_id": envelope["evidence_id"],
                    "operation": envelope["operation"],
                    "signature_alg": envelope["signature"]["alg"],
                    "kid": envelope["signature"]["kid"],
                    "limitations": envelope["limitations"],
                }
                for name, envelope in evidence.items() if envelope
            },
        }
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "arli-demo/0.1"

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            page = (HERE / "index.html").read_bytes()
            self._send(200, page, "text/html; charset=utf-8")
        elif path == "/api/scenario":
            self._json(200, {"scenario": SCENARIO, "floor_basis_points": FLOOR_BASIS_POINTS, "network": NETWORK})
        elif path == "/api/health":
            # A liveness answer, so a check can tell "the engine is up" from "the page is up".
            self._json(200, {"ok": True, "engine": "arli-discovery", "network": NETWORK})
        else:
            self._json(404, {"error": "not found", "path": path})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path != "/api/exchange":
            self._json(404, {"error": "not found", "path": path})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as error:
            self._json(400, {"error": f"bad json: {error}"})
            return
        seeker = [str(item) for item in payload.get("seeker_features") or SCENARIO["seeker"]["features"]]
        holder = [str(item) for item in payload.get("holder_features") or SCENARIO["holder"]["features"]]
        floor = int(payload.get("floor_basis_points") or FLOOR_BASIS_POINTS)
        if not seeker or not holder:
            self._json(400, {"error": "each side needs at least one feature; an empty set matches nothing"})
            return
        try:
            self._json(200, run_exchange(seeker, holder, floor))
        except Exception as error:  # noqa: BLE001
            # An honest failure beats a pretty one: the page shows this text.
            self._json(500, {"error": f"{type(error).__name__}: {error}"})

    def log_message(self, *args, **kwargs) -> None:  # keep the console quiet
        return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8131)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"arli discovery demo on http://127.0.0.1:{args.port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
