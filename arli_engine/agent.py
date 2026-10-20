"""Run one agent at a public address, and keep its key across restarts.

Why this exists. `arli-discovery-agent` used to raise a pair of agents on two ports, which is the
right shape for a conformance suite and the wrong shape for a participant: a room reaches an agent
at an address, and the key behind that address has to be the same key tomorrow. So this module runs
one agent, takes its feature list from the command line, and stores its private key in a file with
owner-only permissions.

Run it:

    arli-discovery-agent --port 8811 --name my-agent \
        --features "skill:brand-design,city:shenzhen"

Then put TLS in front (see docs/host-your-agent.md) and hand the address to a room. The card it
serves names the key, and the identity method signs with it, so the address can prove it holds the
key it publishes.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .adapters.a2a.cards import build_card
from .adapters.a2a.server import AgentState, start_agent
from .core.match import FeatureSet
from .core.signing import KeyPair
from .serve import operations

DEFAULT_PORT = 8811


def load_or_create_key(path: Path) -> tuple[KeyPair, bool]:
    """Load the key at `path`, or create it there. Returns the pair and whether it is new."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if path.exists():
        private_pem = path.read_bytes()
        key = serialization.load_pem_private_key(private_pem, password=None)
        public_pem = key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return KeyPair(private_pem=private_pem, public_pem=public_pem), False

    pair = KeyPair.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Owner-only, and it has to exist with those permissions rather than be tightened after.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(pair.private_pem)
    return pair, True


def build_agent(name: str, features: list[str], base_url: str, *, kid: str | None = None,
                private_pem: bytes | None = None, region: str = "eu-central") -> AgentState:
    """One agent: its signed card, its own feature set, its own empty journals."""
    card = build_card(
        name=name,
        description=f"ARLI discovery agent {name}",
        base_url=base_url,
        skills=[{
            "id": "discover.match",
            "name": "Closed intersection",
            "description": "Match intents without an index holder.",
            "inputModes": ["application/json"],
            "outputModes": ["application/json"],
        }],
        operations=operations(),
        public_features=features,
        kid=kid or f"{name}-key-1",
        private_pem=private_pem,
        region=region,
    )
    return AgentState(
        name,
        card,
        operations(),
        FeatureSet(features),
        region,
        private_pem=private_pem,
        kid=kid or f"{name}-key-1",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arli-discovery-agent",
        description="Run one ARLI discovery agent at an address, with its key kept between restarts.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="port to listen on")
    parser.add_argument("--host", default="127.0.0.1", help="address to bind (use 0.0.0.0 in a container)")
    parser.add_argument("--name", default="", help="the agent's name; defaults to the hostname")
    parser.add_argument("--features", default="",
                        help='comma-separated feature list, e.g. "skill:brand-design,city:shenzhen"')
    parser.add_argument("--base-url", default="",
                        help="the public address this agent is reached at, for its card "
                             "(default http://127.0.0.1:PORT)")
    parser.add_argument("--key-file", default="",
                        help="where the private key lives (default ~/.arli-agent/NAME.pem)")
    parser.add_argument("--region", default="eu-central", help="the region named in the card")
    args = parser.parse_args(argv)

    name = args.name or os.uname().nodename or "arli-agent"
    features = [item.strip() for item in args.features.split(",") if item.strip()]
    key_path = Path(args.key_file).expanduser() if args.key_file else Path(
        f"~/.arli-agent/{name}.pem").expanduser()

    key, created = load_or_create_key(key_path)
    base_url = args.base_url or f"http://127.0.0.1:{args.port}"
    state = build_agent(name, features, base_url, private_pem=key.private_pem, region=args.region)
    server, _thread = start_agent(state, args.port, host=args.host)

    print(
        f"agent {name}: {base_url}\n"
        f"  features: {len(features)} held at home, the card publishes the count only\n"
        f"  key: {key_path} ({'created' if created else 'loaded'}), kid {state.kid}\n"
        f"  card: {base_url}/.well-known/agent-card.json\n"
        f"  capabilities: {base_url}/.well-known/arli-capabilities.json\n"
        f"  health: {base_url}/api/health\n"
        f"  serve it behind TLS and hand the address to a room: "
        f"curl -X POST https://match.arliwork.com/api/connect -H 'Content-Type: application/json' "
        f"-d '{{\"endpoint\":\"{base_url}\",\"kind\":\"supply\"}}'",
        flush=True,
    )
    try:
        import time

        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
