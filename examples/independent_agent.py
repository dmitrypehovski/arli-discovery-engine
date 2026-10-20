"""An independent agent, written from the text, importing nothing from this engine.

Why this file exists. "Any harness can join" is a claim, and claims about a standard are worth
exactly what an independent implementation proves. This file is that implementation for one role:
the holder side of a match. It uses the standard library and `cryptography` only, reads its feature
list from the command line, and knows nothing about our packages.

What it implements, and it is all the text asks of a holder:
  GET  /.well-known/agent-card.json      a card signed with its own key (JWS, ES256, raw R||S)
  GET  /.well-known/arli-capabilities.json   what it does and which lists it honours
  POST /a2a/rpc                          agent/card, arli/capabilities, arli/identity/sign,
                                         arli/negotiation/{open,turn,witness},
                                         arli/opportunity/decide,
                                         arli/psi/{keys,sign,record}
  GET  /api/health                       so a run can tell "up" from "not up"

Run: python examples/independent_agent.py --port 8871 --name my-holder \
        --features "skill:design,city:sf,offer:infra"
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

EXTENSION_VERSION = "1.3.0"
EXTENSION_URI = "https://arliwork.com/a2a/extensions/arli.discovery.v1"
NETWORK = "network-eu-design"
FEATURE_CAP = 64
MAX_TURNS = 12
ACTIONS = ("propose", "counter", "accept", "decline")
TURN_REFUSALS = (
    "not_a_seat", "unknown_action", "already_settled", "turn_limit", "not_your_turn",
    "first_turn_is_propose_or_decline", "seats_must_alternate", "accept_without_offer",
    "propose_over_offer", "not_a_witness", "witness_without_proof", "witness_card_unverified",
    "witness_seat_mismatch", "witness_proof_invalid", "witness_mismatch",
)
OPENING_REFUSALS = ("intent_inactive", "synthetic_not_admitted", "same_owner", "same_intent",
                    "network_mismatch", "types_not_comparable", "below_required_level")
CALL_REFUSALS = ("missing_field", "unknown_method", "parse_error", "bad_type", "nonce_too_short",
                 "purpose_required", "rate_limited")
MIN_NONCE_CHARS = 16
IDENTITY_WINDOW_SECONDS = 60


def canonical(value) -> str:
    """Canonical JSON as the text uses it: sorted keys, no spaces, no float."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


class Refused(Exception):
    """A refusal always carries a code from a closed list."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


class Agent:
    """Everything this agent owns: its key, its own list, one journal per pair, one key per session."""

    def __init__(self, name: str, features: list[str]) -> None:
        self.name = name
        self.features = list(features)
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.kid = f"{name}-key-1"
        self.card = self._build_card()
        self.negotiations: dict[str, dict] = {}
        self.opportunities: dict[str, dict] = {}
        self.psi_sessions: dict[str, rsa.RSAPrivateKey] = {}
        self.identity_requests: dict[tuple[str, str], float] = {}
        self.peer = ""
        self.lock = threading.Lock()

    # --- the card and signatures ---

    def jwk(self) -> dict:
        point = self.key.public_key().public_numbers()
        size = 32
        return {
            "kty": "EC", "crv": "P-256",
            "x": b64u(point.x.to_bytes(size, "big")),
            "y": b64u(point.y.to_bytes(size, "big")),
        }

    def _sign_raw(self, data: bytes) -> str:
        der = self.key.sign(data, ec.ECDSA(hashes.SHA256()))
        r, s = utils.decode_dss_signature(der)
        return b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))

    def _build_card(self) -> dict:
        body = {
            "name": self.name,
            "version": "1.0.0",
            "description": f"Independent agent {self.name}, written from the text",
            "capabilities": {"streaming": False, "pushNotifications": False},
            "skills": [{
                "id": "discover.match", "name": "Closed intersection",
                "description": "Match intents without an index holder.",
                "inputModes": ["application/json"], "outputModes": ["application/json"],
            }],
            "extensions": [{"uri": EXTENSION_URI, "version": EXTENSION_VERSION}],
            "x-arli-public-feature-count": len(self.features),
        }
        header = {"alg": "ES256", "kid": self.kid, "typ": "JOSE", "jwk": self.jwk()}
        protected = b64u(canonical(header).encode("utf-8"))
        payload = b64u(canonical(body).encode("utf-8"))
        signature = self._sign_raw(f"{protected}.{payload}".encode("ascii"))
        body["signatures"] = [{"protected": protected, "signature": signature}]
        return body

    def capabilities(self) -> dict:
        return {
            "extension_uri": EXTENSION_URI,
            "extension_version": EXTENSION_VERSION,
            "implementation": "independent, written from the text, no engine imports",
            "call_rejections": list(CALL_REFUSALS),
            "negotiation": {"actions": list(ACTIONS), "turn_limit": MAX_TURNS,
                            "turn_rejections": list(TURN_REFUSALS)},
            "opening_rejections": list(OPENING_REFUSALS),
            "feature_cap": FEATURE_CAP,
            "matching": {"scheme": "closed_blind_rsa",
                         "limitations": [
                             "Offline enumeration is closed by the scheme.",
                             "Online probing of a single guess is not closed; one exchange per "
                             "session and the feature cap bound it.",
                             "The holder learns how many features were submitted, not their values.",
                         ]},
        }

    def envelope(self, operation: str, parameters: dict, result: dict, limitations: list[str]) -> dict:
        body = {
            "provider": {"id": "independent-agent", "name": "Independent agent", "version": "1.0.0"},
            "operation": operation,
            "parameters": parameters,
            "normalized_result": result,
            "confidence": "observed",
            "authority": "external-service",
            "limitations": limitations,
            "completeness": {"status": "complete_within_window", "conditions": [],
                             "policy_filtered_sections": [], "truncated_sections": [],
                             "unavailable_sections": [], "excluded": [], "dropped_events": {}},
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        body["evidence_id"] = "ev_" + hashlib.sha256(canonical(body).encode("utf-8")).hexdigest()
        body["signature"] = {"alg": "ES256", "kid": self.kid,
                             "value": self._sign_raw(canonical(body).encode("utf-8"))}
        return body

    # --- the methods ---

    def identity_sign(self, params: dict) -> dict:
        nonce = params.get("nonce")
        if not isinstance(nonce, str) or len(nonce) < MIN_NONCE_CHARS:
            raise Refused("nonce_too_short", f"at least {MIN_NONCE_CHARS} characters are required")
        purpose = params.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            raise Refused("purpose_required", "name what the signature is for")
        window = (self.peer, purpose.strip().lower())
        now = time.time()
        last = self.identity_requests.get(window)
        if last is not None and now - last < IDENTITY_WINDOW_SECONDS:
            raise Refused("rate_limited", "one signed nonce per address and purpose per minute")
        self.identity_requests[window] = now
        return {"signature": self._sign_raw(nonce.encode("utf-8")), "kid": self.kid, "alg": "ES256",
                "purpose": purpose.strip(), "signed_bytes": len(nonce.encode("utf-8"))}

    def negotiation_open(self, params: dict) -> dict:
        for field in ("negotiation_id", "peers", "intents"):
            if not params.get(field):
                raise Refused("missing_field", f"{field} is required")
        negotiation_id = params["negotiation_id"]
        peers = list(params["peers"])
        intents = list(params["intents"])
        if len(peers) != 2 or len(intents) != 2:
            raise Refused("bad_type", "two peers and two intents are required")
        network = params.get("network", "")
        admits_synthetic = bool(params.get("admits_synthetic", False))
        required = params.get("required_level")

        def decide() -> str | None:
            for intent in intents:
                if not intent.get("active", True):
                    return "intent_inactive"
            if not admits_synthetic:
                for intent in intents:
                    if intent.get("provenance", "human") == "synthetic":
                        return "synthetic_not_admitted"
            if intents[0].get("owner") == intents[1].get("owner"):
                return "same_owner"
            if intents[0].get("intent_id") == intents[1].get("intent_id"):
                return "same_intent"
            for intent in intents:
                if intent.get("network") != network:
                    return "network_mismatch"
            kinds = [intent.get("type", "person") for intent in intents]
            if not (("supply" in kinds and "demand" in kinds)
                    or kinds == ["person", "person"]):
                return "types_not_comparable"
            if required:
                order = ("declared", "third_party_verified", "topic_owner_verified")
                need = order.index(required) if required in order else 0
                levels = [order.index(intent.get("claim_level", "declared"))
                          if intent.get("claim_level", "declared") in order else 0 for intent in intents]
                if min(levels) < need:
                    return "below_required_level"
            return None

        rejection = decide()
        if rejection:
            return {"negotiation_id": negotiation_id, "opened": False, "rejection": rejection}
        self.negotiations[negotiation_id] = {"seats": peers, "turns": [], "settled": False}
        self.opportunities[negotiation_id] = {"consent": {peers[0]: None, peers[1]: None}}
        return {"negotiation_id": negotiation_id, "opened": True, "seats": peers,
                "initiator": peers[0], "seat_for_me": self.name,
                "counterparty": peers[1] if peers[0] == self.name else peers[0]}

    def _apply_turn(self, negotiation: dict, seat: str, action: str, negotiation_id: str) -> None:
        if action not in ACTIONS:
            raise Refused("unknown_action", action)
        if negotiation["settled"]:
            raise Refused("already_settled", negotiation_id)
        if seat not in negotiation["seats"]:
            raise Refused("not_a_seat", f"{seat} is not a seat here")
        turns = negotiation["turns"]
        if turns:
            if turns[-1][0] == seat:
                raise Refused("seats_must_alternate", "the same seat cannot take two turns in a row")
            if action == "accept" and turns[-1][1] != "propose":
                raise Refused("accept_without_offer", "accept follows a propose")
        elif action in ("accept", "counter"):
            raise Refused("first_turn_is_propose_or_decline", "the initiator owes the first turn")
        if len(turns) >= MAX_TURNS:
            raise Refused("turn_limit", "the limit is reached; the negotiation stays unsettled")
        if action == "accept":
            negotiation["settled"] = True
        turns.append((seat, action))

    def negotiation_turn(self, params: dict) -> dict:
        negotiation_id = params.get("negotiation_id")
        negotiation = self.negotiations.get(negotiation_id)
        if negotiation is None:
            raise Refused("missing_field", f"unknown negotiation {negotiation_id}")
        seat = params.get("seat")
        if seat != self.name:
            raise Refused("not_a_seat", f"this agent holds {self.name}, not {seat}")
        action = params.get("action")
        self._apply_turn(negotiation, seat, action, negotiation_id)
        outcome = "agreed" if negotiation["settled"] else None
        return {"turn": {"seat": seat, "action": action, "message": params.get("message", "")},
                "outcome": outcome,
                "evidence": self.envelope("negotiation_turn",
                                          {"seat": seat, "action": action,
                                           "turn_index": len(negotiation["turns"]),
                                           "turn_limit": MAX_TURNS},
                                          {"outcome": outcome, "turn_count": len(negotiation["turns"])},
                                          ["A turn is recorded once, in this agent's own journal."])}

    def negotiation_witness(self, params: dict) -> dict:
        negotiation_id = params.get("negotiation_id")
        negotiation = self.negotiations.get(negotiation_id)
        if negotiation is None:
            raise Refused("missing_field", f"unknown negotiation {negotiation_id}")
        seat = params.get("seat")
        if seat == self.name:
            raise Refused("not_a_witness", "an agent takes its own turns; witnessing is the other side's")
        if seat not in negotiation["seats"]:
            raise Refused("not_a_seat", f"{seat} is not a seat here")
        proof = params.get("proof")
        if not isinstance(proof, dict):
            raise Refused("witness_without_proof", "the speaker's envelope is required")
        card = params.get("card")
        if not isinstance(card, dict):
            raise Refused("witness_card_unverified", "the speaker's signed card is required")
        key = verify_card_and_key(card)
        if key is None:
            raise Refused("witness_card_unverified", "the card does not verify or carries no key")
        if card.get("name") != seat:
            raise Refused("witness_seat_mismatch", f"the card names {card.get('name')}, seat is {seat}")
        if not verify_envelope(proof, key):
            raise Refused("witness_proof_invalid", "the envelope does not verify with the card's key")
        recorded = proof.get("parameters") or {}
        if recorded.get("seat") != seat or recorded.get("action") != params.get("action"):
            raise Refused("witness_mismatch", "the envelope records another seat or action")
        self._apply_turn(negotiation, seat, params.get("action"), negotiation_id)
        return {"recorded": True, "turns": len(negotiation["turns"]),
                "settled": negotiation["settled"],
                "evidence": self.envelope("negotiation_witness",
                                          {"seat": seat, "action": params.get("action")},
                                          {"turns": len(negotiation["turns"])},
                                          ["A witnessed turn is the counterparty's own."])}

    def opportunity_decide(self, params: dict) -> dict:
        negotiation_id = params.get("negotiation_id")
        opportunity = self.opportunities.get(negotiation_id)
        if opportunity is None:
            raise Refused("missing_field", f"unknown opportunity {negotiation_id}")
        actor = params.get("actor")
        if actor not in opportunity["consent"]:
            raise Refused("not_a_seat", f"{actor} is not a party here")
        if opportunity["consent"][actor] is not None:
            raise Refused("bad_type", "this party already decided")
        opportunity["consent"][actor] = bool(params.get("approve"))
        everyone = all(value is True for value in opportunity["consent"].values())
        return {"opportunity_id": negotiation_id,
                "status": "approved" if everyone else "pending",
                "human_consent": opportunity["consent"]}

    # --- the matching flow, holder side ---

    def psi_keys(self, params: dict) -> dict:
        session_id = params.get("session_id")
        if not session_id:
            raise Refused("missing_field", "session_id is required")
        if len(self.features) > FEATURE_CAP:
            raise Refused("bad_type", f"{len(self.features)} features is above the cap {FEATURE_CAP}")
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.psi_sessions[session_id] = key
        numbers = key.public_key().public_numbers()
        return {"scheme": "closed_blind_rsa", "feature_cap": FEATURE_CAP,
                "n": format(numbers.n, "x"), "e": format(numbers.e, "x")}

    def _token(self, feature: str, key: rsa.RSAPrivateKey) -> int:
        digest = hashlib.sha256(feature.lower().strip().encode("utf-8")).digest()
        return pow(int.from_bytes(digest, "big") % key.public_key().public_numbers().n,
                   key.private_numbers().d, key.public_key().public_numbers().n)

    def psi_sign(self, params: dict) -> dict:
        session_id = params.get("session_id")
        blinded = params.get("blinded")
        key = self.psi_sessions.get(session_id)
        if key is None:
            raise Refused("missing_field", f"no key for session {session_id}")
        if not isinstance(blinded, list):
            raise Refused("bad_type", "blinded must be a list")
        if len(blinded) > FEATURE_CAP:
            raise Refused("bad_type", f"{len(blinded)} values is above the cap {FEATURE_CAP}")
        numbers = key.public_key().public_numbers()
        signed = [format(pow(int(value, 16), key.private_numbers().d, numbers.n), "x")
                  for value in blinded]
        holder_tokens = [format(self._token(feature, key), "x") for feature in self.features]
        return {"session_id": session_id, "signed": signed, "holder_tokens": holder_tokens,
                "note": "Values were signed without being read."}

    def psi_record(self, params: dict) -> dict:
        session_id = params.get("session_id")
        matched = params.get("matched")
        floor = params.get("floor_basis_points")
        if not session_id or matched is None or floor is None:
            raise Refused("missing_field", "session_id, matched and floor_basis_points are required")
        matched = list(matched)
        basis = int(len(matched) * 10000 / max(len(self.features), 1))
        return {"side": self.name, "matched_features": matched, "overlap_basis_points": basis,
                "overlap_ratio": f"{len(matched)}/{len(self.features)}",
                "evidence": self.envelope(
                    "match_recorded",
                    {"floor_basis_points": int(floor), "matched_feature_count": len(matched)},
                    {"matched_feature_count": len(matched)},
                    ["The holder records the same intersection; neither side learns the other's list."])}

    def dispatch(self, method: str, params: dict) -> dict:
        table = {
            "agent/card": lambda _p: {"card": self.card},
            "arli/capabilities": lambda _p: self.capabilities(),
            "arli/identity/sign": self.identity_sign,
            "arli/negotiation/open": self.negotiation_open,
            "arli/negotiation/turn": self.negotiation_turn,
            "arli/negotiation/witness": self.negotiation_witness,
            "arli/opportunity/decide": self.opportunity_decide,
            "arli/psi/keys": self.psi_keys,
            "arli/psi/sign": self.psi_sign,
            "arli/psi/record": self.psi_record,
        }
        handler = table.get(method)
        if handler is None:
            raise Refused("unknown_method", f"unknown method: {method}")
        return handler(params)


def verify_card_and_key(card: dict) -> ec.EllipticCurvePublicKey | None:
    """Verify a card by its own rules and return the key from its header, or None."""
    entries = card.get("signatures") or []
    if not entries:
        return None
    protected = entries[0].get("protected") or ""
    signature = entries[0].get("signature") or ""
    try:
        header = json.loads(base64.urlsafe_b64decode(protected + "=" * (-len(protected) % 4)))
        jwk = header["jwk"]
        x = int.from_bytes(base64.urlsafe_b64decode(jwk["x"] + "=" * (-len(jwk["x"]) % 4)), "big")
        y = int.from_bytes(base64.urlsafe_b64decode(jwk["y"] + "=" * (-len(jwk["y"]) % 4)), "big")
        key = ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
        body = {name: value for name, value in card.items() if name != "signatures"}
        payload = b64u(canonical(body).encode("utf-8"))
        raw = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        key.verify(der, f"{protected}.{payload}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
        return key
    except (InvalidSignature, KeyError, ValueError, TypeError):
        return None


def verify_envelope(envelope: dict, key: ec.EllipticCurvePublicKey) -> bool:
    """Verify an envelope signature: the body without the signature, signed as sent."""
    entry = envelope.get("signature") or {}
    value = entry.get("value")
    if not value:
        return False
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        if len(raw) != 64:
            return False
        der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
        body = {name: item for name, item in envelope.items() if name != "signature"}
        key.verify(der, canonical(body).encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def make_handler(agent: Agent):
    class Handler(BaseHTTPRequestHandler):
        server_version = "independent-agent/1.0"

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
            if path == "/.well-known/agent-card.json":
                self._json(200, agent.card)
            elif path == "/.well-known/arli-capabilities.json":
                self._json(200, agent.capabilities())
            elif path == "/api/health":
                self._json(200, {"ok": True, "agent": agent.name, "features": len(agent.features)})
            else:
                self._json(404, {"error": "not found", "path": path})

        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/a2a/rpc":
                self._json(404, {"error": "not found", "path": self.path})
                return
            length = int(self.headers.get("Content-Length") or 0)
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as error:
                self._json(400, {"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": f"parse_error: {error}"}})
                return
            req_id = request.get("id")
            method = request.get("method") or ""
            agent.peer = self.client_address[0] if self.client_address else ""
            try:
                with agent.lock:
                    result = agent.dispatch(method, request.get("params") or {})
                self._json(200, {"jsonrpc": "2.0", "id": req_id, "result": result})
            except Refused as refusal:
                self._json(200, {"jsonrpc": "2.0", "id": req_id,
                                 "error": {"code": -32602,
                                           "message": f"{refusal.code}: {refusal.detail}"}})
            except Exception as error:  # noqa: BLE001
                self._json(200, {"jsonrpc": "2.0", "id": req_id,
                                 "error": {"code": -32603, "message": f"internal: {error}"}})

        def log_message(self, *args, **kwargs) -> None:
            return

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8871)
    parser.add_argument("--name", default="independent-holder")
    parser.add_argument("--features", default="skill:design,city:sf,offer:infra,role:investor")
    args = parser.parse_args()
    features = [item.strip() for item in args.features.split(",") if item.strip()]
    if not features:
        print("at least one feature is required", file=sys.stderr)
        return 1
    agent = Agent(args.name, features)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(agent))
    print(f"independent agent {args.name} on http://127.0.0.1:{args.port} "
          f"with {len(features)} features, extension {EXTENSION_VERSION}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
