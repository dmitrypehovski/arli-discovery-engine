"""A wrapper over the packaged agent stand: kept out of habit, the logic is in the package.

    .venv/bin/python tools/serve_pair.py [port A] [port B]

Serves until interrupted. Second process: the conformance suite:

    python -m arli_engine.conformance --target http://127.0.0.1:8801 --holder http://127.0.0.1:8802
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arli_engine.adapters.a2a.cards import build_card  # noqa: E402
from arli_engine.adapters.a2a.server import AgentState, start_agent  # noqa: E402
from arli_engine.core.capability import EFFECT_KEYS, Operation  # noqa: E402
from arli_engine.core.match import FeatureSet  # noqa: E402
from arli_engine.core.signing import KeyPair  # noqa: E402


def operations() -> list[Operation]:
    every = {key: False for key in EFFECT_KEYS}
    every["may_access_network"] = True
    return [
        Operation(name="discover.match", description="Match intents", effects=every),
        Operation(name="act.as.other", description="Act for another principal", effects=every,
                  availability_code="disabled_by_policy",
                  availability_reason="An agent acts only for its own principal."),
    ]


def main() -> int:
    port_a = int(sys.argv[1]) if len(sys.argv) > 1 else 8801
    port_b = int(sys.argv[2]) if len(sys.argv) > 2 else 8802
    # The stand lives in the package: here we only parse two numbers for the familiar command.
    from arli_engine.serve import serve

    serve(port_a, port_b)
    return 0


def _legacy() -> int:
    key_a, key_b = KeyPair.generate(), KeyPair.generate()

    def make(name: str, key: KeyPair, port: int, features: list[str]) -> AgentState:
        card = build_card(
            name=name, description=f"ARLI engine agent {name}", base_url=f"http://127.0.0.1:{port}",
            skills=[{"id": "discover.match", "name": "Closed intersection", "description": "d",
                     "inputModes": ["application/json"], "outputModes": ["application/json"]}],
            operations=operations(), public_features=features, kid=f"{name}-key-1",
            private_pem=key.private_pem,
        )
        return AgentState(name, card, operations(), FeatureSet(features), "eu-central",
                          private_pem=key.private_pem, kid=f"{name}-key-1")

    state_a = make("arli-agent-a", key_a, port_a, ["role:founder", "skill:design", "city:sf"])
    state_b = make("arli-agent-b", key_b, port_b, ["skill:design", "city:sf", "role:investor"])
    state_a.peer_keys = {"arli-agent-b": key_b.public_pem}
    state_b.peer_keys = {"arli-agent-a": key_a.public_pem}
    start_agent(state_a, port_a)
    start_agent(state_b, port_b)
    print(f"engine agents: http://127.0.0.1:{port_a} and http://127.0.0.1:{port_b}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    raise SystemExit(main())
