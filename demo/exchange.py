"""One exchange driver, used by both the demonstration and the matching product.

Why it is a separate module. The demonstration and the product must run exactly the same protocol,
or the product would be a different thing dressed in the same words. Both call this, so there is one
implementation of the exchange and one place where its claim can break.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arli_engine.client import AgentClient  # noqa: E402
from arli_engine.core.signing import verify_card_self  # noqa: E402
from arli_engine.serve import build_pair, port_of  # noqa: E402

NETWORK = "network-eu-design"


def clip(value, limit: int = 150) -> str:
    """Long blinded numbers are cut for display only; the count is always reported."""
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Transcript:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.wire: list[dict] = []

    def step(self, name: str, actor: str, request: dict, response: dict, note: str) -> None:
        self.steps.append({
            "name": name,
            "actor": actor,
            "note": note,
            "request": clip(request),
            "response": clip(response),
        })
        self.wire.append({"name": name, "payload": json.dumps(request, ensure_ascii=False)})


def run_exchange(
    seeker_features: list[str],
    holder_features: list[str],
    floor: int,
    *,
    seeker_name: str = "arli-agent-a",
    holder_name: str = "arli-agent-b",
    network: str = NETWORK,
    session_id: str | None = None,
    seeker_kind: str = "demand",
    holder_kind: str = "supply",
) -> dict:
    """One full exchange between two live agents: open, turns, consent, blind matching, records."""
    states, servers = build_pair(0, 0, names=(seeker_name, holder_name))
    transcript = Transcript()
    try:
        states[0].features = type(states[0].features)(seeker_features)
        states[1].features = type(states[1].features)(holder_features)
        port_a, port_b = port_of(servers[0]), port_of(servers[1])
        seat_a, seat_b = states[0].name, states[1].name

        with AgentClient(f"http://127.0.0.1:{port_a}") as seeker, \
                AgentClient(f"http://127.0.0.1:{port_b}") as holder:

            cards = []
            for who, client in (("seeker", seeker), ("holder", holder)):
                card = client.get_card()
                verified, reasons = verify_card_self(card)
                cards.append({"who": who, "name": card.get("name"), "verified": verified, "reasons": reasons})

            session = session_id or f"exchange-{abs(hash((seat_a, seat_b, floor))) % 10**8}"
            open_params = {
                "negotiation_id": session,
                "peers": [seat_a, seat_b],
                "network": network,
                "admits_synthetic": False,
                "intents": [
                    {"intent_id": f"{seat_a}-{seeker_kind}", "owner": seat_a, "type": seeker_kind,
                     "network": network, "provenance": "human", "active": True,
                     "claim_level": "declared"},
                    {"intent_id": f"{seat_b}-{holder_kind}", "owner": seat_b, "type": holder_kind,
                     "network": network, "provenance": "human", "active": True,
                     "claim_level": "declared"},
                ],
            }
            opened = seeker.call("arli/negotiation/open", open_params)
            holder.call("arli/negotiation/open", open_params)
            transcript.step(
                "Open the pair", "seeker",
                {"negotiation_id": session, "network": network,
                 "intents": [{"type": seeker_kind, "owner": seat_a}, {"type": holder_kind, "owner": seat_b}]},
                {"opened": opened.get("opened"), "seats": opened.get("seats"),
                 "initiator": opened.get("initiator"), "rejection": opened.get("rejection")},
                "A pair opens only because the two intents face each other: a demand and a supply in "
                "the same network, held by different sides. Two supplies would be competition and "
                "would be refused with a code.",
            )
            if not opened.get("opened"):
                # A pair that does not face is not a weak match, it is not a pair. Saying so, with the
                # code the engine returned, is the honest answer: no exchange, nothing compared.
                return {
                    "cards": cards,
                    "steps": transcript.steps,
                    "session_id": session,
                    "opened": False,
                    "rejection": opened.get("rejection"),
                    "agreed": False,
                    "matched_features": [],
                    "overlap_basis_points": None,
                    "overlap_ratio": None,
                    "floor_basis_points": floor,
                    "passed_floor": False,
                    "leak_check": {
                        "checked_steps": len(transcript.wire),
                        "features_on_each_side": [len(seeker_features), len(holder_features)],
                        "clean": True,
                    },
                    "leaked_features": [],
                    "evidence": {},
                }

            propose = seeker.call("arli/negotiation/turn", {
                "negotiation_id": session, "seat": seat_a, "action": "propose",
                "message": {"scope": "matching only, no data exchange"},
            })
            transcript.step(
                "First turn", "seeker",
                {"seat": seat_a, "action": "propose", "message": {"scope": "matching only, no data exchange"}},
                {"turn": propose["turn"]["action"], "outcome": propose.get("outcome")},
                "The grammar is closed: the first turn can be a proposal or a decline, nothing else.",
            )
            accept = holder.call("arli/negotiation/turn", {
                "negotiation_id": session, "seat": seat_b, "action": "accept",
                "message": {"agreed": True},
            })
            transcript.step(
                "Second turn", "holder",
                {"seat": seat_b, "action": "accept", "message": {"agreed": True}},
                {"turn": accept["turn"]["action"], "outcome": accept.get("outcome")},
                "Seats must alternate, and accept is legal only right after a proposal.",
            )
            agreed = accept.get("outcome") == "agreed"

            decision_a = seeker.call("arli/opportunity/decide", {
                "negotiation_id": session, "actor": seat_a, "approve": True,
            })
            decision_b = holder.call("arli/opportunity/decide", {
                "negotiation_id": session, "actor": seat_b, "approve": True,
            })
            transcript.step(
                "Human decision", "both",
                {"actor": "each side answers on its own address", "approve": True},
                {"consent": decision_b.get("human_consent") or decision_a.get("human_consent"),
                 "status": decision_b.get("status") or decision_a.get("status")},
                "Agents agreeing never opens a relationship. Each side's person decides as a separate "
                "act, and both answers are recorded.",
            )

            keys = holder.call("arli/psi/keys", {"session_id": session})
            transcript.step(
                "Publish the pair key", "holder", {"session_id": session},
                {"scheme": keys.get("scheme"), "feature_cap": keys.get("feature_cap"),
                 "modulus_bits": len(str(keys.get("n", ""))) * 4, "e": keys.get("e")},
                "Only the public half of the key leaves the holder. The private half never moves, and "
                "the key lives for this session only.",
            )
            commit = seeker.call("arli/psi/commit", {
                "session_id": session, "peer_name": seat_b, "n": keys["n"], "e": keys["e"],
            })
            transcript.step(
                "Blind and send", "seeker",
                {"session_id": session, "peer_name": seat_b, "blinded": commit.get("blinded")},
                {"count": len(commit.get("blinded", [])), "note": commit.get("note")},
                "This is what leaves the seeker: blinded numbers. No feature goes on the wire. Without "
                "the private key they cannot be read back, and the holder cannot test a guess against "
                "them offline.",
            )
            signed = holder.call("arli/psi/sign", {"session_id": session, "blinded": commit["blinded"]})
            transcript.step(
                "Sign blind", "holder",
                {"session_id": session, "blinded": commit.get("blinded")},
                {"signed_count": len(signed.get("signed", [])),
                 "holder_tokens_count": len(signed.get("holder_tokens", []))},
                "The holder signs values it cannot read, and adds its own signed values. Neither list "
                "can be compared by one side alone.",
            )
            finish = seeker.call("arli/psi/finish", {
                "session_id": session, "n": keys["n"], "signed": signed["signed"],
                "holder_tokens": signed["holder_tokens"], "floor_basis_points": floor,
            })
            basis = int(finish.get("overlap_basis_points") or 0)
            transcript.step(
                "Unblind and compare", "seeker", {"session_id": session, "floor_basis_points": floor},
                {"matched_features": finish.get("matched_features"),
                 "overlap_ratio": finish.get("overlap_ratio"),
                 "overlap_basis_points": basis},
                "The seeker learns only the intersection among its own features, and only above the "
                "floor it declared itself. The holder's list stays closed.",
            )
            record = holder.call("arli/psi/record", {
                "session_id": session, "matched": finish.get("matched_features", []),
                "floor_basis_points": floor,
            })
            transcript.step(
                "Record on the other side", "holder",
                {"session_id": session, "matched_count": len(record.get("matched_features", []))},
                {"matched_features": record.get("matched_features"),
                 "overlap_basis_points": record.get("overlap_basis_points")},
                "Both sides hold a signed envelope over the same intersection, so neither can claim "
                "afterwards that a different match happened.",
            )

            envelopes = {
                "turn": propose.get("evidence"),
                "finish": finish.get("evidence"),
                "record": record.get("evidence"),
            }

        leaked = []
        for item in transcript.wire:
            for side, features in ((seat_a, seeker_features), (seat_b, holder_features)):
                for feature in features:
                    if re.search(re.escape(feature.strip()), item["payload"], re.IGNORECASE):
                        leaked.append({"step": item["name"], "side": side, "feature": feature})

        return {
            "cards": cards,
            "steps": transcript.steps,
            "session_id": session,
            "opened": True,
            "rejection": None,
            "agreed": agreed,
            "matched_features": finish.get("matched_features", []),
            "overlap_basis_points": basis,
            "overlap_ratio": finish.get("overlap_ratio"),
            "floor_basis_points": floor,
            "passed_floor": basis >= floor,
            "leak_check": {
                "checked_steps": len(transcript.wire),
                "features_on_each_side": [len(seeker_features), len(holder_features)],
                "clean": not leaked,
            },
            "leaked_features": leaked,
            "evidence": {
                name: {
                    "evidence_id": envelope["evidence_id"],
                    "operation": envelope["operation"],
                    "signature_alg": envelope["signature"]["alg"],
                    "kid": envelope["signature"]["kid"],
                    "limitations": envelope["limitations"],
                }
                for name, envelope in envelopes.items()
                if envelope
            },
        }
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
