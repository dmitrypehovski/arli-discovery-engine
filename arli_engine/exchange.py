"""One exchange opened directly at another agent's address, with nothing in the middle.

Why this lives in the engine and not in the service. An exchange between two agents is protocol work: a
card to verify, seats to name, turns to make and witness, two approvals, one intersection. The room does
the same thing on behalf of a caller, but a room is a place, not a requirement. This module is the shape
where the initiative is the participant's own.

One side runs locally for the length of the exchange and reaches the other at its address. Neither list
crosses: only the calls the extension defines travel, and both sides keep their own journal.

Command line:

    python -m arli_engine.exchange \
        --peer https://their-agent.example.com \
        --features "need:warehouse,city:shenzhen" \
        --kind demand --peer-kind supply --floor 3000
"""

from __future__ import annotations

import argparse
import json
import sys

from .adapters.a2a.server import start_agent
from .agent import build_agent
from .client import AgentClient
from .core.signing import KeyPair, verify_card_self

DEFAULT_NETWORK = "network-eu-design"
MAX_FEATURES = 64


def direct_exchange(
    peer: str,
    my_features: list[str],
    *,
    floor: int,
    my_kind: str = "demand",
    peer_kind: str = "supply",
    my_name: str = "you",
    network: str = DEFAULT_NETWORK,
) -> dict:
    """Run one full exchange with the agent at `peer`. Returns the result, refusals included."""
    if not my_features:
        raise ValueError("at least one feature is required: an empty set matches nothing")
    if len(my_features) > MAX_FEATURES:
        raise ValueError(f"{len(my_features)} features is above the cap of {MAX_FEATURES} per exchange")
    if floor < 0 or floor > 10000:
        raise ValueError(f"floor must be between 0 and 10000, not {floor}")

    # The name has to be the seat this side takes: an agent may only hold its own seat, and naming it
    # "you-ephemeral" while opening with a seat called "you" is refused, correctly.
    # A signed card, because the counterparty witnesses our turns against it: our side is ephemeral,
    # so the key is fresh for each exchange and never reused as an identity.
    key = KeyPair.generate()
    state = build_agent(my_name, list(my_features), "http://127.0.0.1:0", private_pem=key.private_pem)
    server, _thread = start_agent(state, 0, host="127.0.0.1")
    local = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        peer_card = AgentClient(peer).get_card()
        verified, reasons = verify_card_self(peer_card)
        if not verified:
            return {"opened": False, "rejection": "peer_card_unverified",
                    "detail": "; ".join(reasons) or "signature does not verify"}
        peer_name = str(peer_card.get("name") or peer)
        seat_a, seat_b = my_name, peer_name
        session = f"direct-{abs(hash((peer, floor, tuple(my_features)))) % 10**8}"

        with AgentClient(local) as mine, AgentClient(peer) as theirs:
            open_params = {
                "negotiation_id": session, "peers": [seat_a, seat_b], "network": network,
                "admits_synthetic": False,
                "intents": [
                    {"intent_id": f"{seat_a}-{my_kind}", "owner": seat_a, "type": my_kind,
                     "network": network, "provenance": "human", "active": True,
                     "claim_level": "declared"},
                    {"intent_id": f"{seat_b}-{peer_kind}", "owner": seat_b, "type": peer_kind,
                     "network": network, "provenance": "human", "active": True,
                     "claim_level": "declared"},
                ],
            }
            tried = []
            opened = mine.call("arli/negotiation/open", open_params)
            if not opened.get("opened"):
                return {"opened": False, "rejection": str(opened.get("rejection")),
                        "id": seat_b, "name": seat_b}
            theirs.call("arli/negotiation/open", open_params)

            mine_card = mine.get_card()
            propose = mine.call("arli/negotiation/turn",
                                {"negotiation_id": session, "seat": seat_a, "action": "propose",
                                 "message": {"scope": "matching only"}})
            theirs.call("arli/negotiation/witness",
                        {"negotiation_id": session, "seat": seat_a, "action": "propose",
                         "message": {"scope": "matching only"},
                         "proof": propose["evidence"], "card": mine_card})
            tried.append(json.dumps(propose.get("turn"), ensure_ascii=False))

            accept = theirs.call("arli/negotiation/turn",
                                 {"negotiation_id": session, "seat": seat_b, "action": "accept",
                                  "message": {"agreed": True}})
            mine.call("arli/negotiation/witness",
                      {"negotiation_id": session, "seat": seat_b, "action": "accept",
                       "message": {"agreed": True},
                       "proof": accept["evidence"], "card": peer_card})

            mine.call("arli/opportunity/decide", {"negotiation_id": session, "actor": seat_a, "approve": True})
            theirs.call("arli/opportunity/decide", {"negotiation_id": session, "actor": seat_b, "approve": True})

            keys = theirs.call("arli/psi/keys", {"session_id": session})
            commit = mine.call("arli/psi/commit",
                               {"session_id": session, "peer_name": seat_b,
                                "n": keys["n"], "e": keys["e"]})
            signed = theirs.call("arli/psi/sign",
                                 {"session_id": session, "blinded": commit["blinded"]})
            finish = mine.call("arli/psi/finish",
                               {"session_id": session, "n": keys["n"], "signed": signed["signed"],
                                "holder_tokens": signed["holder_tokens"],
                                "floor_basis_points": floor})
            record = theirs.call("arli/psi/record",
                                 {"session_id": session, "matched": finish.get("matched_features", []),
                                  "floor_basis_points": floor})

        basis = int(finish.get("overlap_basis_points") or 0)
        return {
            "opened": True, "rejection": None, "session_id": session,
            "id": seat_b, "name": seat_b,
            "agreed": accept.get("outcome") == "agreed",
            "overlap_basis_points": basis,
            "overlap_ratio": finish.get("overlap_ratio"),
            "matched_features": finish.get("matched_features", []),
            "passed_floor": basis >= floor and bool(finish.get("matched_features")),
            "their_side": {"overlap_basis_points": record.get("overlap_basis_points"),
                           "overlap_ratio": record.get("overlap_ratio")},
            "wire": tried,
            "note": "One exchange, opened by us at their address. Nothing held by a third party.",
        }
    finally:
        server.shutdown()
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m arli_engine.exchange",
        description="Run one exchange directly with another agent, without a room.",
    )
    parser.add_argument("--peer", required=True, help="the other agent's public address")
    parser.add_argument("--features", required=True, help="your list, comma separated")
    parser.add_argument("--kind", default="demand", choices=["demand", "supply", "person"])
    parser.add_argument("--peer-kind", default="supply", choices=["demand", "supply", "person"])
    parser.add_argument("--floor", type=int, default=3000, help="minimum overlap, in basis points")
    parser.add_argument("--name", default="you", help="the name your side takes in the exchange")
    args = parser.parse_args(argv)

    features = [item.strip() for item in args.features.split(",") if item.strip()]
    try:
        result = direct_exchange(args.peer, features, floor=args.floor, my_kind=args.kind,
                                 peer_kind=args.peer_kind, my_name=args.name)
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"opened": False, "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("opened") else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())