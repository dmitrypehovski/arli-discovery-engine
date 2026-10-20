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
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from demo.exchange import NETWORK, run_exchange  # noqa: E402

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


# The exchange itself lives in demo/exchange.py: the demonstration and the matching product must
# run the same protocol, or the product would be a different thing wearing the same words.


class Handler(BaseHTTPRequestHandler):
    server_version = "arli-demo/0.2"

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
