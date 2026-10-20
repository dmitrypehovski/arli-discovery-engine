"""Local agents from the package itself: start the engine from outside, without our tree.

Why this lives in the package rather than in the `tools` directory. The phase 6 gate
reads: a third-party developer installs the engine and passes the conformance suite in
an evening. An installed package with no way to start an agent fails that gate: a person
has the engine and the suite, but no implementation to run the suite against. So the
stand ships inside.

This is not a production service and should not pretend to be one: two agents in one
memory, keys per process, example features. The limitations are listed out loud.
"""

from __future__ import annotations

import argparse
import time
from http.server import ThreadingHTTPServer

from arli_engine.adapters.a2a.cards import build_card
from arli_engine.adapters.a2a.server import AgentState, start_agent
from arli_engine.core.capability import EFFECT_KEYS, Operation
from arli_engine.core.match import FeatureSet
from arli_engine.core.signing import KeyPair

DEFAULT_PORTS = (8841, 8842)

# The declared default floor: forty percent overlap.
FLOOR_DEFAULT = 4000

# Example features. They are here so that the intersection is non-empty and the
# conformance suite checks the scheme rather than emptiness. There is no real personal
# data in this file.
FEATURES_A = ["role:founder", "skill:design", "city:sf", "need:infra"]
FEATURES_B = ["skill:design", "city:sf", "offer:infra", "role:investor"]

LIMITATIONS = [
    "A local stand for conformance runs, not a service: two agents share one process and one memory.",
    "Keys are generated per process and are not persisted.",
    "The example features are synthetic. No real personal data is used.",
]


def operations() -> list[Operation]:
    every = {key: False for key in EFFECT_KEYS}
    every["may_access_network"] = True
    return [
        Operation(name="discover.match", description="Match intents", effects=every),
        Operation(
            name="act.as.other",
            description="Act for another principal",
            effects=every,
            availability_code="disabled_by_policy",
            availability_reason="An agent acts only for its own principal.",
        ),
    ]


def build_pair(
    port_a: int = 0, port_b: int = 0, *, names: tuple[str, str] = ("arli-agent-a", "arli-agent-b")
) -> tuple[list[AgentState], list[ThreadingHTTPServer]]:
    """Build two agents and start them. Port 0 means "any free port".

    Returns the states and the servers, so the actual port is read from the server
    rather than guessed: on port zero it cannot be guessed.
    """
    key_a, key_b = KeyPair.generate(), KeyPair.generate()
    name_a, name_b = names
    # One journal per pair, shared by both sides. Without it the second side's turn is invisible
    # to the first, and a cross-side negotiation never settles.
    shared_journal: dict = {}
    shared_opportunities: dict = {}

    def make(name: str, key: KeyPair, port: int, features: list[str]) -> AgentState:
        card = build_card(
            name=name,
            description=f"ARLI engine agent {name}",
            base_url=f"http://127.0.0.1:{port}",
            skills=[{
                "id": "discover.match",
                "name": "Closed intersection",
                "description": "Match intents without an index holder.",
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
            }],
            operations=operations(),
            public_features=features,
            kid=f"{name}-key-1",
            private_pem=key.private_pem,
        )
        return AgentState(
            name,
            card,
            operations(),
            FeatureSet(features),
            "eu-central",
            private_pem=key.private_pem,
            kid=f"{name}-key-1",
        )

    state_a = make(name_a, key_a, port_a, FEATURES_A)
    state_b = make(name_b, key_b, port_b, FEATURES_B)
    state_a.negotiations = shared_journal
    state_b.negotiations = shared_journal
    state_a.opportunities = shared_opportunities
    state_b.opportunities = shared_opportunities
    state_a.peer_keys = {name_b: key_b.public_pem}
    state_b.peer_keys = {name_a: key_a.public_pem}
    server_a, _ = start_agent(state_a, port_a)
    server_b, _ = start_agent(state_b, port_b)
    return [state_a, state_b], [server_a, server_b]


def port_of(server: ThreadingHTTPServer) -> int:
    return int(server.server_address[1])


def serve(port_a: int = DEFAULT_PORTS[0], port_b: int = DEFAULT_PORTS[1]) -> None:
    """Start a pair and serve until interrupted."""
    _states, servers = build_pair(port_a, port_b)
    real_a, real_b = (port_of(server) for server in servers)
    print(
        f"agents: http://127.0.0.1:{real_a} and http://127.0.0.1:{real_b}\n"
        f"conformance: arli-discovery-conformance --target http://127.0.0.1:{real_a} "
        f"--holder http://127.0.0.1:{real_b}",
        flush=True,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arli-discovery-agent",
        description="Run two local ARLI discovery agents so a conformance suite has something to check.",
    )
    parser.add_argument("--port-a", type=int, default=DEFAULT_PORTS[0], help="port of the first agent")
    parser.add_argument("--port-b", type=int, default=DEFAULT_PORTS[1], help="port of the second agent")
    args = parser.parse_args(argv)
    serve(args.port_a, args.port_b)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
