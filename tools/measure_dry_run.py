"""Calibrating the measurer on live agents, without people.

What this is and what it is not. This runs the measurer over real envelopes issued by
real engine agents, so that by pilot time we have a working method and a table. These
are NOT pilot numbers: the features in the stand are synthetic, there are no people
here, and these numbers must not be presented as a funnel. The pilot per the plan runs
on people who consented.

Run: python -m tools.measure_dry_run
Result: out/measure_dry_run.json and a table in the output.
"""

from __future__ import annotations

import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arli_engine.client import AgentClient, AgentRefused  # noqa: E402
from arli_engine.core.crypto import public_pem_from_private, verify_envelope  # noqa: E402
from arli_engine.measure import measure  # noqa: E402
from arli_engine.serve import FLOOR_DEFAULT, build_pair, port_of  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "out" / "measure_dry_run.json"


def main() -> int:
    states, servers = build_pair(0, 0)
    ports = [port_of(server) for server in servers]
    url_a, url_b = (f"http://127.0.0.1:{port}" for port in ports)
    # Issuer keys: we check both keys against every envelope rather than guessing the author.
    candidate_keys = {state.name: public_pem_from_private(state.private_pem) for state in states}
    public_keys: dict[str, bytes] = {}

    envelopes: list[dict] = []
    refusals: list[dict] = []
    matches: list[dict] = []
    intents: list[dict] = []

    try:
        with AgentClient(url_a) as client_a, AgentClient(url_b) as client_b:
            # 1. A full closed-intersection exchange: only the matches among our own features.
            session_id = secrets.token_hex(6)
            holder_keys = client_b.arli_psi_keys({"session_id": session_id})
            commit = client_a.arli_psi_commit({
                "session_id": session_id, "peer_name": states[1].name,
                "n": holder_keys["n"], "e": holder_keys["e"],
            })
            signed = client_b.arli_psi_sign({"session_id": session_id, "blinded": commit["blinded"]})
            match_a = client_a.arli_psi_finish({
                "session_id": session_id, "n": holder_keys["n"], "signed": signed["signed"],
                "holder_tokens": signed["holder_tokens"], "floor_basis_points": FLOOR_DEFAULT,
            })
            match_b = client_b.arli_psi_record({
                "session_id": session_id, "matched": match_a["matched_features"],
                "floor_basis_points": FLOOR_DEFAULT,
            })
            envelopes.extend([match_a["evidence"], match_b["evidence"]])
            matches.append({
                "passed_floor": bool(match_a["evidence"]["parameters"].get("passed_floor")),
                "matched": len(match_a["matched_features"]),
            })
            for side in ("a", "b"):
                intents.append({"intent_id": f"unit-{side}", "closed": bool(match_a["matched_features"])})

            # 2. An exchange with a declared but unreachable floor: the finding is not
            # returned rather than padded up to the limit.
            tight_session = secrets.token_hex(6)
            tight_keys = client_b.arli_psi_keys({"session_id": tight_session})
            tight_commit = client_a.arli_psi_commit({
                "session_id": tight_session, "peer_name": states[1].name,
                "n": tight_keys["n"], "e": tight_keys["e"],
            })
            tight_signed = client_b.arli_psi_sign({
                "session_id": tight_session, "blinded": tight_commit["blinded"],
            })
            tight_match = client_a.arli_psi_finish({
                "session_id": tight_session, "n": tight_keys["n"], "signed": tight_signed["signed"],
                "holder_tokens": tight_signed["holder_tokens"], "floor_basis_points": 9900,
            })
            envelopes.append(tight_match["evidence"])
            matches.append({
                "passed_floor": bool(tight_match["evidence"]["parameters"].get("passed_floor")),
                "matched": len(tight_match["matched_features"]),
            })

            # 3. Refusals: each must arrive as a code from the closed list.
            try:
                client_a.arli_registry_register({
                    "kid": "k-dry", "principal_id": "p-dry", "public_pem": "pem-dry",
                })
                client_a.arli_registry_rotate({
                    "kid": "k-dry", "new_kid": "k-dry-2", "new_public_pem": "pem-dry-2",
                    "reason": "because-i-said-so",
                })
                refusals.append({"code": None})
            except AgentRefused as exc:
                refusals.append({"code": exc.code})

            same_owner = client_a.arli_negotiation_open({
                "negotiation_id": "dry-same-owner", "peers": [states[0].name, states[1].name],
                "network": "dry-run",
                "intents": [
                    {"intent_id": "i1", "owner": states[0].name, "network": "dry-run", "type": "person"},
                    {"intent_id": "i2", "owner": states[0].name, "network": "dry-run", "type": "person"},
                ],
            })
            refusals.append({"code": same_owner.get("rejection")})

            synthetic = client_a.arli_negotiation_open({
                "negotiation_id": "dry-synthetic", "peers": [states[0].name, states[1].name],
                "network": "dry-run",
                "intents": [
                    {"intent_id": "i1", "owner": states[0].name, "network": "dry-run",
                     "type": "person", "provenance": "synthetic"},
                    {"intent_id": "i2", "owner": states[1].name, "network": "dry-run", "type": "person"},
                ],
            })
            refusals.append({"code": synthetic.get("rejection")})
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()

    # A key for each issuer: the one that actually verifies the envelope signature.
    for envelope in envelopes:
        issuer = str(envelope.get("provider", {}).get("id"))
        for owner, pem in candidate_keys.items():
            verified, _errors = verify_envelope(envelope, pem)
            if verified:
                public_keys[issuer] = pem
                break

    record = {
        "public_keys": {name: pem.decode() for name, pem in public_keys.items()},
        "envelopes": envelopes,
        "refusals": refusals,
        "intents": intents,
        "matches": matches,
        # Decisions, human labels and participant state are absent here on purpose:
        # without people these quantities are unmeasured, and the measurer will say so.
    }
    report = measure(record, source="dry-run on synthetic features, not a pilot")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "caveat": (
            "Instrument calibration on synthetic features with no people. "
            "These are not pilot numbers and must not be presented as a funnel."
        ),
        **report.to_json(),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print("measurer calibration, synthetic features, no people")
    print(report.as_table())
    print(f"written: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
