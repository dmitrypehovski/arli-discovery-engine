"""A live run of the stand: two agents, signed cards, local matching,
bounded negotiation and separate human consent.

Run (from the repository root):
    .venv/bin/python examples/two_agents/main.py

Writes evidence envelopes to out/ and prints a step-by-step report.
"""

from __future__ import annotations

import base64
import json
import secrets
import sys
from pathlib import Path

import httpx

import sys as _sys
from pathlib import Path as _Path

# The example runs from the repository root, so we add the root to the path ourselves.
_ROOT = _Path(__file__).resolve().parents[2]
if str(_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_ROOT))

from arli_engine import (
    EXTENSION_URI,
    FeatureSet,
    KeyPair,
    NegotiationError,
    Operation,
    build_card,
    verify_card,
)
from arli_engine.core.capability import EFFECT_KEYS
from arli_engine.core.crypto import verify_envelope
from arli_engine.core.psi import feature_hash
from arli_engine.adapters.a2a.server import CAPS_PATH, CARD_PATH, RPC_PATH, AgentState, start_agent

OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)

# The network in which the match is sought: pair eligibility is checked within one network.
NETWORK = "network-eu-design"

# The floor is declared by the caller: below it the finding is not returned at all.
# 4000 is forty percent of shared features.
FLOOR = 4000

STEPS: list[str] = []


def step(text: str) -> None:
    STEPS.append(text)
    print(text, flush=True)


def rpc(port: int, method: str, params: dict | None = None) -> dict:
    response = httpx.post(
        f"http://127.0.0.1:{port}{RPC_PATH}",
        json={"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}},
        timeout=10,
    )
    payload = response.json()
    if "error" in payload:
        raise NegotiationError(payload["error"]["message"])
    if "result" not in payload:
        # A reply with neither a result nor an error is a surface defect, not a quiet emptiness.
        raise RuntimeError(f"{method} returned neither result nor error: {payload}")
    return payload["result"]


def rpc_error(port: int, method: str, params: dict | None = None) -> str:
    response = httpx.post(
        f"http://127.0.0.1:{port}{RPC_PATH}",
        json={"jsonrpc": "2.0", "id": method, "method": method, "params": params or {}},
        timeout=10,
    )
    payload = response.json()
    return payload.get("error", {}).get("message", "") if "error" in payload else ""


def operations(name: str) -> list[Operation]:
    """One operation is available, the second is honestly unavailable with a reason."""
    return [
        Operation(
            name="discover.match",
            description="Compare intent features with a counterpart using salted commitments",
            effects={
                "mutates_artifact": False,
                "launches_process": False,
                "may_show_ui": False,
                "may_access_network": True,
                "may_write_filesystem": True,
                "changes_permissions": False,
                "requires_root": False,
            },
            limitations=(
                "Counterparty can test guesses about coarse features.",
                "Returns a signal, not a proof of fit.",
            ),
        ),
        Operation(
            name="directory.impersonate",
            description="Act under another principal's identity",
            effects={"may_access_network": True, "changes_permissions": True},
            availability_code="disabled_by_policy",
            availability_reason="Attribution invariant: an agent acts only for its own principal.",
        ),
    ]


def main() -> int:
    key_a, key_b = KeyPair.generate(), KeyPair.generate()
    port_a, port_b = 8801, 8802

    features_a = FeatureSet(["role:founder", "skill:design", "city:sf", "need:infra partner"])
    features_b = FeatureSet(["skill:design", "city:sf", "offer:infra", "role:investor"])

    card_a = build_card(
        name="arli-agent-a",
        description="ARLI prototype agent A: verified discovery over A2A",
        base_url=f"http://127.0.0.1:{port_a}",
        skills=[
            {
                "id": "discover.match",
                "name": "Salted commitment matching",
                "description": "Find overlap between intent features without a third party",
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
                "examples": ["role:founder + skill:design"],
            }
        ],
        operations=operations("a"),
        public_features=list(features_a),
        kid="arli-a-key-1",
        private_pem=key_a.private_pem,
    )
    card_b = build_card(
        name="arli-agent-b",
        description="ARLI prototype agent B: verified discovery over A2A",
        base_url=f"http://127.0.0.1:{port_b}",
        skills=[
            {
                "id": "discover.match",
                "name": "Salted commitment matching",
                "description": "Find overlap between intent features without a third party",
                "inputModes": ["application/json"],
                "outputModes": ["application/json"],
                "examples": ["skill:design + city:sf"],
            }
        ],
        operations=operations("b"),
        public_features=list(features_b),
        kid="arli-b-key-1",
        private_pem=key_b.private_pem,
    )

    # The negotiation log is shared: by our design the protocol itself is the server for this log.
    shared_negotiations: dict = {}
    shared_opportunities: dict = {}
    state_a = AgentState("arli-agent-a", card_a, operations("a"), features_a, "eu-central",
                         negotiations=shared_negotiations, opportunities=shared_opportunities,
                         private_pem=key_a.private_pem, kid="arli-a-key-1",
                         peer_keys={"arli-agent-b": key_b.public_pem})
    state_b = AgentState("arli-agent-b", card_b, operations("b"), features_b, "eu-central",
                         negotiations=shared_negotiations, opportunities=shared_opportunities,
                         private_pem=key_b.private_pem, kid="arli-b-key-1",
                         peer_keys={"arli-agent-a": key_a.public_pem})
    server_a, _ = start_agent(state_a, port_a)
    server_b, _ = start_agent(state_b, port_b)

    evidence: dict[str, dict] = {}
    try:
        # 1. The card at the standard A2A address and signature verification.
        fetched_b = httpx.get(f"http://127.0.0.1:{port_b}{CARD_PATH}", timeout=10).json()
        trusted = {"arli-b-key-1": key_b.public_pem}
        kid = fetched_b["signatures"][0]["protected"]
        header = json.loads(base64.urlsafe_b64decode(kid + "=" * (-len(kid) % 4)))
        ok, reasons = verify_card(fetched_b, trusted[header["kid"]])
        step(f"1. Card of agent B fetched from {CARD_PATH}; signature: {'valid' if ok else 'INVALID'} {reasons or ''}")
        step(f"   extension: {EXTENSION_URI}")

        # 2. Tampering with the card must break the signature.
        tampered = dict(fetched_b)
        tampered["description"] = "ARLI prototype agent B: trusted by everyone"
        ok_t, reasons_t = verify_card(tampered, key_b.public_pem)
        step(f"2. Tampering with the card description: signature {'passed (BAD)' if ok_t else 'rejected'} {reasons_t}")

        # 3. Capabilities: available and unavailable with a reason; an undeclared effect is assumed possible.
        caps = httpx.get(f"http://127.0.0.1:{port_b}{CAPS_PATH}", timeout=10).json()["manifest"]
        first = caps["operations"][0]
        unavailable = [o for o in caps["operations"] if not o["available"]]
        step(f"3. Operations: {caps['operation_count']}; unavailable: {len(unavailable)} with reason: {unavailable[0]['availability_reason']}")
        step(f"   the available operation declares {len(first['declared_effect_keys'])} of {len(EFFECT_KEYS)} effects, the rest are treated as possible")

        # 4. Closed intersection: the holder publishes its public key, the applicant
        # blinds its features, the holder signs, the applicant unblinds and
        # gets only the matches among its own features.
        session_id = secrets.token_hex(6)
        holder_keys = rpc(port_b, "arli/psi/keys", {"session_id": session_id})
        step(f"4. Closed intersection, scheme {holder_keys['scheme']}, feature cap {holder_keys['feature_cap']}")
        commit = rpc(port_a, "arli/psi/commit", {
            "session_id": session_id,
            "peer_name": "arli-agent-b",
            "n": holder_keys["n"],
            "e": holder_keys["e"],
        })
        step(f"   the applicant sent blinded values: {len(commit['blinded'])}, the holder sees only their count")
        signed = rpc(port_b, "arli/psi/sign", {
            "session_id": session_id,
            "blinded": commit["blinded"],
        })
        match_a = rpc(port_a, "arli/psi/finish", {
            "session_id": session_id,
            "n": holder_keys["n"],
            "signed": signed["signed"],
            "holder_tokens": signed["holder_tokens"],
            "floor_basis_points": FLOOR,
        })
        match_b = rpc(port_b, "arli/psi/record", {
            "session_id": session_id,
            "matched": match_a["matched_features"],
            "floor_basis_points": FLOOR,
        })
        evidence["match_a"] = match_a["evidence"]
        evidence["match_b"] = match_b["evidence"]
        step(f"   the floor is declared by the caller: {FLOOR} basis points, passed: {match_a['evidence']['parameters']['passed_floor']}")
        step(f"   matched at A: {match_a['matched_features']} (ratio {match_a['overlap_ratio']}, basis points {match_a['overlap_basis_points']})")
        step(f"   matched at B: {match_b['matched_features']} (ratio {match_b['overlap_ratio']})")
        unmatched_a = sorted(set(map(str.lower, features_a)) - set(match_a["matched_features"]))
        step(f"   what B did not learn about A: {len(unmatched_a)} unmatched features of A stayed inside A")
        step(f"   match envelope: {match_a['evidence']['evidence_id'][:24]}… scheme {match_a['evidence']['parameters']['scheme']}")
        # Envelope signature: without it, evidence is indistinguishable from a forgery.
        sig_ok, sig_errors = verify_envelope(match_a["evidence"], key_a.public_pem)
        tampered = dict(match_a["evidence"])
        tampered["normalized_result"] = dict(tampered["normalized_result"], matched_feature_count=99)
        sig_bad, _ = verify_envelope(tampered, key_a.public_pem)
        step(f"   envelope signature with key A: valid {sig_ok}; after body tampering: {sig_bad} (expected False)")
        if not sig_ok or sig_bad:
            raise RuntimeError(f"envelope signature check failed: {sig_errors}")
        step(f"   the envelope carries no other-side feature count: {'counterpart_feature_count' not in match_a['evidence']['parameters']}")
        # Offline enumeration is closed: the holder's signatures cannot be matched against guesses without its key.
        tokens = {int(v, 16) for v in signed["holder_tokens"]}
        n_holder, e_holder = int(holder_keys["n"], 16), int(holder_keys["e"], 16)
        guessable = [
            g for g in ("skill:design", "city:sf", "role:investor", "city:berlin")
            if pow(feature_hash(g), e_holder, n_holder) in tokens
        ]
        step(f"   offline enumeration of guesses against the received signatures: found {len(guessable)} of 4 (expected 0)")
        if guessable:
            raise RuntimeError(f"closed intersection leaked: {guessable}")

        # 4b. The unit of work is a closed intent, not an introduction: demand
        # matches with supply, two supplies do not match each other,
        # and a synthetic intent does not enter a human network.
        unit_id = "unit-" + secrets.token_hex(3)
        def _open(standings, **extra):
            params = {"negotiation_id": unit_id, "peers": ["arli-agent-a", "arli-agent-b"],
                      "network": NETWORK, "intents": standings}
            params.update(extra)
            return rpc(port_a, "arli/negotiation/open", params)

        supply_demand = _open([
            {"intent_id": "i-supply", "owner": "arli-agent-a", "network": NETWORK,
             "type": "supply", "statement": "a spare line for two weeks"},
            {"intent_id": "i-demand", "owner": "arli-agent-b", "network": NETWORK,
             "type": "demand", "statement": "needs a line for a project"},
        ])
        two_supplies = _open([
            {"intent_id": "i-supply", "owner": "arli-agent-a", "network": NETWORK, "type": "supply"},
            {"intent_id": "i-supply-2", "owner": "arli-agent-b", "network": NETWORK, "type": "supply"},
        ])
        synthetic = _open([
            {"intent_id": "i-supply", "owner": "arli-agent-a", "network": NETWORK,
             "type": "person", "provenance": "synthetic"},
            {"intent_id": "i-demand", "owner": "arli-agent-b", "network": NETWORK, "type": "person"},
        ])
        step(f"4b. Intents: demand with supply opened {supply_demand['opened']}; "
             f"two supplies {two_supplies['opened']} with reason \"{two_supplies.get('rejection')}\"; "
             f"synthetic in a human network {synthetic['opened']} with reason \"{synthetic.get('rejection')}\"")
        if not supply_demand["opened"] or two_supplies.get("rejection") != "types_not_comparable" \
                or synthetic.get("rejection") != "synthetic_not_admitted":
            raise RuntimeError("intent rules did not behave as declared")

        # 5. Negotiation by the grammar, one turn per side.
        # Pair eligibility is checked before opening: both intents are active and attributed
        # to one network. This rule is taken from Index, but here a refusal does not stay silent.
        neg_id = "neg-" + secrets.token_hex(4)
        standing = [
            {"intent_id": "intent-a", "owner": "arli-agent-a", "network": NETWORK},
            {"intent_id": "intent-b", "owner": "arli-agent-b", "network": NETWORK},
        ]
        open_params = {"negotiation_id": neg_id, "peers": ["arli-agent-a", "arli-agent-b"],
                       "network": NETWORK, "intents": standing}
        opened_a = rpc(port_a, "arli/negotiation/open", open_params)
        opened_b = rpc(port_b, "arli/negotiation/open", open_params)
        step(f"5. Pair eligibility: opened {opened_a['opened']} and {opened_b['opened']}, initiator {opened_a['initiator']}")
        plan = [
            ("arli-agent-a", "propose", "We are both in design and in SF. Looking for an infrastructure partner."),
            ("arli-agent-b", "counter", "I offer the infrastructure. Confirm that this is about design and an early launch."),
            ("arli-agent-a", "propose", "Yes, design and an early launch. A short collaboration."),
            ("arli-agent-b", "accept", "It matches. Ready to introduce."),
        ]
        for seat, action, message in plan:
            port = port_a if seat.endswith("-a") else port_b
            result = rpc(port, "arli/negotiation/turn", {
                "negotiation_id": neg_id, "seat": seat, "action": action, "message": message,
            })
            evidence[f"turn_{action}_{seat[-1]}"] = result["evidence"]
        state_pos = state_a.negotiations[neg_id].outcome
        state_pos_b = state_b.negotiations[neg_id].outcome
        step(f"5b. Negotiation: outcome at A {state_pos}, at B {state_pos_b}, logs match: {state_a.negotiations[neg_id].to_json()['turns'] == state_b.negotiations[neg_id].to_json()['turns']}")

        # 6. Grammar refusals that must work.
        wrong_seat = rpc_error(port_b, "arli/negotiation/turn", {
            "negotiation_id": neg_id, "seat": "arli-agent-a", "action": "propose", "message": "",
        })
        neg_id2 = "neg-" + secrets.token_hex(4)
        rpc(port_a, "arli/negotiation/open", {"negotiation_id": neg_id2, "peers": ["arli-agent-a", "arli-agent-b"]})
        rpc(port_b, "arli/negotiation/open", {"negotiation_id": neg_id2, "peers": ["arli-agent-a", "arli-agent-b"]})
        rpc(port_a, "arli/negotiation/turn", {"negotiation_id": neg_id2, "seat": "arli-agent-a", "action": "propose", "message": "x"})
        rpc(port_b, "arli/negotiation/turn", {"negotiation_id": neg_id2, "seat": "arli-agent-b", "action": "counter", "message": "?"})
        accept_after_counter = rpc_error(port_a, "arli/negotiation/turn", {
            "negotiation_id": neg_id2, "seat": "arli-agent-a", "action": "accept", "message": ""})
        step(f"6. Refusal of a foreign seat: \"{wrong_seat[:60]}\"")
        step(f"   Refusal of accept after counter: \"{accept_after_counter[:60]}\"")

        # 6b. An unfit pair does not open, and the refusal arrives as a code with an envelope.
        inactive_standing = [
            {"intent_id": "intent-a", "owner": "arli-agent-a", "network": NETWORK, "active": False},
            {"intent_id": "intent-b", "owner": "arli-agent-b", "network": NETWORK},
        ]
        refused = rpc(port_a, "arli/negotiation/open", {
            "negotiation_id": "neg-refused-" + secrets.token_hex(2),
            "peers": ["arli-agent-a", "arli-agent-b"],
            "network": NETWORK,
            "intents": inactive_standing,
        })
        evidence["opening_refused"] = refused["evidence"]
        step(f"6b. Unfit pair: opened {refused['opened']}, reason \"{refused['rejection']}\", "
             f"envelope {refused['evidence']['evidence_id'][:24]}…")

        # 7. Human consent separate from agent agreement.
        decide_a = rpc(port_a, "arli/opportunity/decide", {"negotiation_id": neg_id, "actor": "arli-agent-a", "approve": True})
        step(f"7. After the agents agree, status: {decide_a['status']} (A approved, B not yet)")
        decide_b = rpc(port_b, "arli/opportunity/decide", {"negotiation_id": neg_id, "actor": "arli-agent-b", "approve": True})
        step(f"   After the second approval: {decide_b['status']}")
        twice = rpc_error(port_b, "arli/opportunity/decide", {"negotiation_id": neg_id, "actor": "arli-agent-b", "approve": True})
        step(f"   A repeated decision was rejected: \"{twice[:60]}\"")

        summary = {
            "steps": STEPS,
            "evidence_ids": {k: v["evidence_id"] for k, v in evidence.items()},
            "cards": {"a": card_a, "b": fetched_b},
            "manifest_b": caps,
            "match": {"a": match_a, "b": match_b},
            "negotiation": state_a.negotiations[neg_id].to_json(),
            "opportunity_status": decide_b["status"],
            "limits": {
                "prototype": True,
                "matching_leak": "a counterparty can test guesses about coarse features",
                "no_third_party": "neither side sends raw features, both match locally",
            },
        }
        (OUT / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
        for name, env in evidence.items():
            (OUT / f"evidence_{name}.json").write_text(json.dumps(env, ensure_ascii=False, indent=1), encoding="utf-8")
        step(f"8. Envelopes written: {len(evidence)} to {OUT}/summary.json")
    finally:
        server_a.shutdown()
        server_b.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
