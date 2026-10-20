"""The agent's HTTP surface: card, capabilities, matching, negotiation.

The server holds only its own state. Outward it serves the card at the standard A2A
address, a separate capability document and JSON-RPC for the rest.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .cards import capability_document
from arli_engine.core.capability import Operation
from arli_engine.core.crypto import derive_pair_key, pair_label, sign_envelope, verify_envelope
from arli_engine.core.evidence import Completeness, Evidence
from arli_engine.core.match import (
    MATCH_REJECTIONS,
    FeatureSet,
    MatchRejected,
    closed_match,
    closed_match_holder,
    local_match,
)
from arli_engine.core.psi import (  # noqa: F401
    EpochKey,
    MAX_FEATURES_PER_EXCHANGE,
    Keys,
    blind_set,
    generate_keys,
    sign,
    own_signed_tokens,
    sign as rsa_sign,
    unblind_set,
)
from arli_engine.core.claims import (
    CLAIM_LEVELS,
    CLAIM_REJECTIONS,
    CLAIM_STATES,
    ClaimRegistry,
    FeatureClaim,
)
from arli_engine.core.registry import Registry
from arli_engine.core.signing import sign_bytes, verify_card_self
from arli_engine.core.surface import CALL_REJECTIONS, METHOD_NAMES
from arli_engine.core.retention import Retention
from arli_engine.core.negotiate import (
    witness,
    IntentStanding,
    Negotiation,
    NegotiationError,
    NegotiationRejected,
    Opportunity,
    decide_opening,
)

OPENING_LIMITATIONS = [
    "Eligibility is checked on declared standing, not on a live read of the network.",
    "A refused pair is not ranked against other pairs; it is simply not opened.",
]

CARD_PATH = "/.well-known/agent-card.json"
CAPS_PATH = "/.well-known/arli-capabilities.json"
MIN_NONCE_CHARS = 16
IDENTITY_WINDOW_SECONDS = 60
RPC_PATH = "/a2a/rpc"
HEALTH_PATH = "/api/health"


@dataclass
class AgentState:
    """Everything the agent owns on this harness.

    The negotiation log can be shared between two parties: by our design the protocol
    itself is the server for that log. If no shared storage is passed in, the agent
    keeps its own log.
    """

    name: str
    card: dict
    operations: list[Operation]
    features: FeatureSet
    region: str
    negotiations: dict[str, Negotiation] = field(default_factory=dict)
    opportunities: dict[str, Opportunity] = field(default_factory=dict)
    seen_commitments: dict[str, list[str]] = field(default_factory=dict)
    # The pair key is derived through agreement, so the agent needs its own private
    # key and the peer's public key. The pair key never travels over the wire.
    private_pem: bytes | None = None
    kid: str = ""
    # The identity method must not become a signing oracle: one signed nonce per
    # address and purpose per minute, and the asking party's address is what bounds it.
    peer: str = ""
    identity_requests: dict[tuple[str, str], float] = field(default_factory=dict)
    peer_keys: dict[str, bytes] = field(default_factory=dict)
    # Closed-intersection state: the holder's keys per session, the applicant's
    # blindings and the recorded results. Big numbers travel as hex strings, otherwise
    # clients in other languages would lose precision.
    psi_keys: dict[str, Keys] = field(default_factory=dict)
    # One key per epoch, not per exchange: key generation costs more than the exchange, and the holder's
    # own tokens are computed once per epoch instead of once per seeker.
    epoch: "EpochKey | None" = None
    # Which epoch's key was handed to which session: the epoch must not outlive its binding.
    psi_issued: dict[str, str] = field(default_factory=dict)
    psi_blinds: dict[str, tuple[list[str], list[int]]] = field(default_factory=dict)
    psi_results: dict[str, list[str]] = field(default_factory=dict)
    # Key registry and retention: rotation separate from principal revocation,
    # deletion marking separate from physical purge.
    registry: Registry = field(default_factory=Registry)
    retention: Retention = field(default_factory=Retention)
    # Feature claims: issuers, their keys and declared claims.
    claims: ClaimRegistry = field(default_factory=ClaimRegistry)
    declared_claims: dict[str, FeatureClaim] = field(default_factory=dict)


def health_document(state: AgentState) -> dict:
    """What a run needs to tell "up" from "not up", without a card fetch."""
    from .cards import extension_version

    return {"ok": True, "agent": state.name, "extension_version": extension_version(),
            "features_held_at_home": len(state.features), "kid": state.kid}


def current_epoch(state: AgentState, now: float):
    """The agent's epoch, created when there is none or when the old one expired.

    An epoch carries a key and the holder's own tokens. Expiry is not decoration: a token table that lives
    for ever is a dictionary an attacker can accumulate against, and a revoked agent must stop being served.
    """
    from arli_engine.core.psi import DEFAULT_EPOCH_SECONDS, new_epoch

    epoch = state.epoch
    if epoch is None or epoch.expired(now):
        ttl = DEFAULT_EPOCH_SECONDS
        epoch = new_epoch(ttl)
        state.epoch = epoch
    return epoch


def _rpc_ok(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_err(req_id, code, message) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def _card_public_pem(card: dict) -> bytes | None:
    """The public key of the card's own signature header, as PEM, for verifying its turns."""
    import base64

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    signatures = card.get("signatures") or []
    if not signatures:
        return None
    protected = signatures[0].get("protected") or ""
    try:
        header = json.loads(base64.urlsafe_b64decode(protected + "=" * (-len(protected) % 4)))
    except Exception:  # noqa: BLE001
        return None
    jwk = header.get("jwk") or {}
    if not jwk.get("x") or not jwk.get("y"):
        return None

    def decode(text: str) -> bytes:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))

    try:
        key = ec.EllipticCurvePublicNumbers(
            int.from_bytes(decode(jwk["x"]), "big"),
            int.from_bytes(decode(jwk["y"]), "big"),
            ec.SECP256R1(),
        ).public_key()
    except Exception:  # noqa: BLE001
        return None
    return key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)


def _methods(state: AgentState) -> dict:
    def _pair_key(params: dict) -> tuple[str, str, str]:
        """Derive the pair key for the session from the request. The key does not leave."""
        if state.private_pem is None:
            raise ValueError("this agent has no private key: pair key cannot be derived")
        peer_name = params.get("peer_name")
        session_id = params.get("session_id")
        if not peer_name or not session_id:
            raise ValueError("peer_name and session_id are required")
        peer_public = state.peer_keys.get(peer_name)
        if peer_public is None:
            # A refusal with a code from the matching list, not a bare message: the caller has
            # to tell "you have no key for that side" from "the engine broke".
            raise MatchRejected("missing_peer_key", f"no public key known for {peer_name!r}")
        return (
            derive_pair_key(
                state.private_pem,
                peer_public,
                session_id=session_id,
                agent_a=state.name,
                agent_b=peer_name,
            ),
            session_id,
            pair_label(state.name, peer_name),
        )

    def _signed(envelope: dict) -> dict:
        """Sign the evidence envelope with the agent key, if there is one."""
        if state.private_pem is None or not state.kid:
            return envelope
        return sign_envelope(envelope, state.private_pem, state.kid)

    def card(params: dict) -> dict:
        return {"card": state.card}

    def capabilities(params: dict) -> dict:
        return capability_document(state.operations)

    def commitments(params: dict) -> dict:
        pair_key, session_id, pair = _pair_key(params)
        own = state.features.commitments(pair_key, session_id=session_id, pair=pair)
        state.seen_commitments[session_id] = sorted(own.values())
        return {
            "session_id": session_id,
            "side": state.name,
            "commitments": sorted(own.values()),
            "note": (
                "features are never sent; commitments are bound to the session and to the pair, "
                "and the pair key is derived, not transmitted"
            ),
        }

    def match(params: dict) -> dict:
        counterpart = params.get("counterpart_commitments")
        if counterpart is None:
            raise ValueError("counterpart_commitments is required")
        pair_key, session_id, pair = _pair_key(params)
        own = state.features.commitments(pair_key, session_id=session_id, pair=pair)
        policy = []
        if params.get("reveal_identity") is not True:
            policy.append(("identity", "not_approved_for_this_session", None))
        policy.append(("own_unmatched_features", "never_returned_by_design", None))
        outcome = local_match(
            side=state.name,
            own=state.features,
            own_commitments=own,
            counterpart_commitments=list(counterpart),
            session_id=session_id,
            policy_filtered=policy,
        )
        result = outcome.to_json()
        result["evidence"] = _signed(result["evidence"])
        return result

    def psi_holder_keys(params: dict) -> dict:
        """The holder publishes the public part of the key for this session."""
        session_id = params.get("session_id")
        if not session_id:
            raise MatchRejected("missing_pair_key", "session_id is required")
        import time as _time

        epoch = current_epoch(state, _time.time())
        # A session is bound to the epoch that issued its key. Signing without that binding, or with a
        # binding whose epoch has expired, is refused: a seeker that never fetched a key blinded its values
        # with something else entirely, and pairing it against this key would be work with no honest outcome.
        state.psi_issued[session_id] = epoch.epoch_id
        n, e = epoch.keys.public()
        return {
            "session_id": session_id,
            "n": format(n, "x"),
            "e": format(e, "x"),
            "scheme": "closed_blind_rsa",
            "epoch_id": epoch.epoch_id,
            "epoch_expires_in_seconds": max(0, int(epoch.ttl_seconds - (_time.time() - epoch.created_at))),
            "feature_cap": MAX_FEATURES_PER_EXCHANGE,
            "note": "the private exponent never leaves this agent; the key lasts one epoch and then expires",
        }

    def psi_commit(params: dict) -> dict:
        """The applicant blinds its features and sends the holder only those."""
        session_id = params.get("session_id")
        peer_name = params.get("peer_name")
        if not session_id or not peer_name:
            raise MatchRejected("missing_pair_key", "session_id and peer_name are required")
        if not params.get("n") or not params.get("e"):
            raise MatchRejected("missing_peer_key", "n and e are required to blind")
        n, e = int(params["n"], 16), int(params["e"], 16)
        own = list(state.features)
        if len(own) > MAX_FEATURES_PER_EXCHANGE:
            raise MatchRejected(
                "too_many_features", f"{len(own)} features, cap is {MAX_FEATURES_PER_EXCHANGE}"
            )
        blinded, factors = blind_set(own, n, e)
        state.psi_blinds[session_id] = (own, factors)
        return {
            "session_id": session_id,
            "blinded": [format(v, "x") for v in blinded],
            "note": "blinded values only: the holder learns the count, not the features",
        }

    def psi_sign(params: dict) -> dict:
        """The holder signs what was sent and hands out signatures over its own features."""
        session_id = params.get("session_id")
        blinded = params.get("blinded")
        if not session_id or not blinded:
            raise MatchRejected("missing_pair_key", "session_id and blinded are required")
        import time as _time

        epoch = current_epoch(state, _time.time())
        issued = state.psi_issued.get(session_id)
        if issued is None:
            raise MatchRejected("missing_private_key", "no key was issued for this session yet")
        if issued != epoch.epoch_id:
            raise MatchRejected(
                "missing_private_key",
                "the epoch that issued this session's key has expired; keys are per epoch, not for ever",
            )
        if len(blinded) > MAX_FEATURES_PER_EXCHANGE:
            raise MatchRejected(
                "too_many_features", f"{len(blinded)} values, cap is {MAX_FEATURES_PER_EXCHANGE}"
            )
        # Signing the seeker's values is per-exchange work nobody else can do for us. Our own tokens come
        # from the epoch cache and are computed once for every seeker in this epoch.
        signed = [sign(int(v, 16), epoch.keys.d, epoch.keys.n) for v in blinded]
        tokens = epoch.tokens_for(list(state.features))
        return {
            "session_id": session_id,
            "epoch_id": epoch.epoch_id,
            "signed": [format(v, "x") for v in signed],
            "holder_tokens": [format(v, "x") for v in tokens],
            "note": "signatures only: guesses cannot be matched against them without the private exponent",
        }

    def psi_finish(params: dict) -> dict:
        """The applicant unblinds and obtains only the matches among its own features."""
        session_id = params.get("session_id")
        if not session_id or session_id not in state.psi_blinds:
            raise MatchRejected("missing_pair_key", "no pending blinding for this session")
        if not params.get("signed") or params.get("holder_tokens") is None:
            raise MatchRejected("missing_peer_key", "signed and holder_tokens are required")
        if params.get("floor_basis_points") is None:
            # The floor must be declared by the caller: without it there is nothing
            # to tell a good match from a filler.
            raise MatchRejected("missing_floor", "floor_basis_points is required")
        own, factors = state.psi_blinds[session_id]
        signed = [int(v, 16) for v in params["signed"]]
        tokens = {int(v, 16) for v in params["holder_tokens"]}
        if len(signed) != len(factors):
            raise MatchRejected("length_mismatch", f"{len(signed)} values for {len(factors)} features")
        n = int(params.get("n", "0"), 16)
        if not n:
            raise MatchRejected("missing_peer_key", "n is required to unblind")
        unblinded = unblind_set(signed, factors, n)
        policy = []
        if params.get("reveal_identity") is not True:
            policy.append(("identity", "not_approved_for_this_session", None))
        outcome = closed_match(
            side=state.name,
            own=state.features,
            unblinded=unblinded,
            holder_tokens=tokens,
            session_id=session_id,
            floor_basis_points=int(params["floor_basis_points"]),
            policy_filtered=policy,
        )
        result = outcome.to_json()
        result["evidence"] = _signed(result["evidence"])
        return result

    def psi_record(params: dict) -> dict:
        """The holder records the matches the applicant found and hands out its envelope."""
        session_id = params.get("session_id")
        matched = params.get("matched")
        if not session_id or matched is None:
            raise MatchRejected("missing_pair_key", "session_id and matched are required")
        if params.get("floor_basis_points") is None:
            raise MatchRejected("missing_floor", "floor_basis_points is required")
        state.psi_results[session_id] = sorted(str(m) for m in matched)
        outcome = closed_match_holder(
            side=state.name,
            own=state.features,
            matched=list(state.psi_results[session_id]),
            session_id=session_id,
            floor_basis_points=int(params["floor_basis_points"]),
        )
        result = outcome.to_json()
        result["evidence"] = _signed(result["evidence"])
        return result

    def _evidence(operation: str, parameters: dict, result: dict, *, provider: str) -> dict:
        """An envelope for registry and retention operations: they are claims about the world too."""
        envelope = Evidence(
            provider={"id": f"arli-{provider}", "name": f"ARLI {provider}", "version": "1"},
            operation=operation,
            parameters=parameters,
            normalized_result=result,
            confidence="observed",
            authority="local-calculation",
            limitations=[
                "The registry is local to this agent; a network-wide revocation needs a shared root.",
                "Retention covers envelope metadata only: raw features never reach the server.",
            ],
        ).to_json()
        return _signed(envelope)

    def claims_status(params: dict) -> dict:
        return {
            "claims": state.claims.to_json(),
            "declared": [claim.to_json() for claim in state.declared_claims.values()],
            "levels": list(CLAIM_LEVELS),
            "states": list(CLAIM_STATES),
            "claim_rejections": list(CLAIM_REJECTIONS),
            "issuance": (
                "This engine relays and verifies statements made by others. "
                "It does not issue confirmations of its own; that question is with counsel."
            ),
        }

    def claims_issuers(params: dict) -> dict:
        name = params.get("name")
        public_pem = params.get("public_pem")
        if not name or not public_pem:
            raise ValueError("name and public_pem are required")
        state.claims.add_issuer(str(name), str(public_pem).encode())
        return {
            "issuers": sorted(state.claims.issuers),
            "evidence": _evidence(
                "claims_issuer_registered", {"name": str(name)}, {"issuers": sorted(state.claims.issuers)},
                provider="claims",
            ),
        }

    def claims_declare(params: dict) -> dict:
        feature = params.get("feature")
        level = params.get("level")
        if not feature or not level:
            raise ValueError("feature and level are required")
        from datetime import datetime, timezone

        checked_at = params.get("checked_at")
        valid_until = params.get("valid_until")
        claim = FeatureClaim(
            feature=str(feature),
            level=str(level),
            issuer=str(params.get("issuer", "")),
            checked_at=datetime.fromisoformat(str(checked_at)) if checked_at else datetime.now(timezone.utc),
            valid_until=datetime.fromisoformat(str(valid_until)) if valid_until else None,
            issuer_evidence=params.get("issuer_evidence"),
            state=str(params.get("state", "active")),
        )
        # A claim rejection is returned as a code, not an error: the caller needs a
        # reason, not an "internal error", otherwise it cannot tell a bad claim from
        # an engine failure.
        from arli_engine.core.claims import ClaimRejected

        try:
            verified, reasons = state.claims.verify_issuer_envelope(claim)
        except ClaimRejected as exc:
            verified, reasons = False, [exc.code]
        state.declared_claims[claim.feature] = claim
        return {
            "claim": claim.to_json(),
            "verified": verified,
            "reasons": reasons,
            "evidence": _evidence(
                "claims_declare", {"feature": claim.feature, "level": claim.level},
                {"verified": verified}, provider="claims",
            ),
        }

    def registry_status(params: dict) -> dict:
        return {"registry": state.registry.to_json()}

    def registry_register(params: dict) -> dict:
        kid = params.get("kid")
        principal_id = params.get("principal_id")
        public_pem = params.get("public_pem")
        if not kid or not principal_id or not public_pem:
            raise ValueError("kid, principal_id and public_pem are required")
        record = state.registry.register(
            kid, principal_id, public_pem, valid_days=params.get("valid_days")
        )
        return {"key": record.to_json(), "valid": state.registry.check(kid)[0]}

    def registry_rotate(params: dict) -> dict:
        kid, new_kid, new_public_pem = params.get("kid"), params.get("new_kid"), params.get("new_public_pem")
        if not kid or not new_kid or not new_public_pem:
            raise ValueError("kid, new_kid and new_public_pem are required")
        old, new = state.registry.rotate(
            kid, new_kid, new_public_pem, reason=str(params.get("reason", "rotated"))
        )
        return {
            "old_key": old.to_json(),
            "new_key": new.to_json(),
            "old_valid": state.registry.check(kid)[0],
            "new_valid": state.registry.check(new_kid)[0],
            "principal_revoked": bool(state.registry.principals),
            "evidence": _evidence(
                "registry_rotate",
                {"kid": kid, "new_kid": new_kid},
                {"old_valid": False, "new_valid": True},
                provider="registry",
            ),
        }

    def registry_revoke(params: dict) -> dict:
        reason = str(params.get("reason", ""))
        if not reason:
            raise ValueError("reason is required")
        if params.get("principal_id"):
            record = state.registry.revoke_principal(str(params["principal_id"]), reason)
            result = {"principal_id": record.principal_id, "active": record.active}
        elif params.get("kid"):
            record = state.registry.revoke_key(str(params["kid"]), reason)
            result = {"kid": record.kid, "revoked_at": record.revoked_at.isoformat() if record.revoked_at else None}
        else:
            raise ValueError("either kid or principal_id is required")
        return {
            "revoked": result,
            "evidence": _evidence("registry_revoke", {"reason": reason}, result, provider="registry"),
        }

    def registry_confirm(params: dict) -> dict:
        principal_id = params.get("principal_id")
        if not principal_id:
            raise ValueError("principal_id is required")
        record = state.registry.confirm_principal(str(principal_id))
        return {"principal_id": record.principal_id, "active": record.active}

    def registry_check(params: dict) -> dict:
        kid = params.get("kid")
        if not kid:
            raise ValueError("kid is required")
        ok, reasons = state.registry.check(str(kid))
        return {"kid": kid, "valid": ok, "reasons": reasons}

    def retention_status(params: dict) -> dict:
        return {"retention": state.retention.to_json()}

    def retention_record(params: dict) -> dict:
        evidence_id, intent_id = params.get("evidence_id"), params.get("intent_id")
        if not evidence_id or not intent_id:
            raise ValueError("evidence_id and intent_id are required")
        record = state.retention.store(str(evidence_id), str(intent_id))
        return {"record": record.to_json()}

    def retention_archive(params: dict) -> dict:
        intent_id = params.get("intent_id")
        if not intent_id:
            raise ValueError("intent_id is required")
        touched = state.retention.archive_intent(str(intent_id))
        return {
            "intent_id": intent_id,
            "marked_deleted": touched,
            "searchable": state.retention.searchable(),
            "evidence": _evidence(
                "retention_archive", {"intent_id": intent_id}, {"marked_deleted": touched},
                provider="retention",
            ),
        }

    def retention_delete(params: dict) -> dict:
        evidence_id = params.get("evidence_id")
        if not evidence_id:
            raise ValueError("evidence_id is required")
        record = state.retention.delete_envelope(str(evidence_id))
        return {"record": record.to_json()}

    def retention_purge(params: dict) -> dict:
        removed = state.retention.purge()
        return {
            "removed": removed,
            "retention_days": state.retention.days,
            "evidence": _evidence("retention_purge", {}, {"removed": removed}, provider="retention"),
        }

    def negotiation_open(params: dict) -> dict:
        negotiation_id = params.get("negotiation_id")
        peers = params.get("peers")
        if not negotiation_id or not peers or len(peers) != 2:
            raise ValueError("negotiation_id and two peers are required")
        if state.name not in peers:
            raise ValueError("an agent may only hold its own seat in a negotiation")
        other = [p for p in peers if p != state.name][0]
        # The order of the parties sets the initiator: the first turn is always
        # peers[0], so the logs of both sides match.
        seats = (peers[0], peers[1])

        # Pair eligibility is checked before opening and before the first turn. A
        # refusal here does not stay silent: it is returned as a code from the closed
        # list together with an envelope, so the side keeps a trace of why the pair
        # was not opened.
        network = params.get("network")
        intents = params.get("intents") or []
        if network is not None and len(intents) == 2:
            # Intents arrive in the order of the parties: the first belongs to peers[0].
            # The owner of an intent and a seat in a negotiation are different notions,
            # so equal owners are expressible and must be rejected as same_owner.
            a, b = [
                IntentStanding(
                    intent_id=item["intent_id"],
                    owner=item["owner"],
                    network=item["network"],
                    active=bool(item.get("active", True)),
                    type=str(item.get("type", "person")),
                    provenance=str(item.get("provenance", "human")),
                    claim_level=str(item.get("claim_level", "declared")),
                )
                for item in intents
            ]
            # The network declares whether it admits synthetic intents. By default the
            # network is human-facing: a synthetic intent does not enter it.
            code = decide_opening(
                a, b, network,
                admits_synthetic=bool(params.get("admits_synthetic", False)),
                required_level=(str(params["required_level"]) if params.get("required_level") else None),
            )
            if code is not None:
                completeness = Completeness()
                completeness.dropped_events = {"candidates": 0}
                completeness.exclude("pair", "refused_before_opening", None)
                evidence = Evidence(
                    provider={
                        "id": "arli-opening-rules",
                        "name": "ARLI pair eligibility",
                        "version": "1",
                    },
                    operation="negotiation_open",
                    parameters={"peers": list(peers), "network": network},
                    normalized_result={"opened": False, "rejection": code},
                    confidence="derived",
                    authority="local-calculation",
                    limitations=list(OPENING_LIMITATIONS),
                    completeness=completeness,
                )
                return {
                    "negotiation_id": negotiation_id,
                    "opened": False,
                    "rejection": code,
                    "evidence": _signed(evidence.to_json()),
                }

        negotiation = Negotiation(seats=seats)
        state.negotiations[negotiation_id] = negotiation
        state.opportunities[negotiation_id] = Opportunity(actors=seats, negotiation=negotiation)
        return {
            "negotiation_id": negotiation_id,
            "opened": True,
            "seats": list(seats),
            "initiator": peers[0],
            "seat_for_me": state.name,
            "counterparty": other,
        }

    def negotiation_turn(params: dict) -> dict:
        negotiation_id = params.get("negotiation_id")
        if negotiation_id not in state.negotiations:
            raise KeyError(f"unknown negotiation: {negotiation_id}")
        seat = params.get("seat")
        # The agent acts only for its own seat: an attempt to play another's is
        # rejected with a code from the closed list, not free text.
        if not isinstance(seat, str) or seat != state.name:
            raise NegotiationRejected(
                "not_a_seat", f"this agent holds seat {state.name}, not {seat!r}"
            )
        result = state.negotiations[negotiation_id].turn(
            seat, params["action"], params.get("message", "")
        )
        result["evidence"] = _signed(result["evidence"])
        return result

    def negotiation_witness(params: dict) -> dict:
        """Record the counterparty's turn. The rules live in the core, the wire stays thin."""
        negotiation_id = params.get("negotiation_id")
        if negotiation_id not in state.negotiations:
            raise KeyError(f"unknown negotiation: {negotiation_id}")
        result = witness(
            state.negotiations[negotiation_id],
            state.name,
            params.get("seat"),
            params.get("action"),
            params.get("message", ""),
            params.get("proof"),
            params.get("card"),
        )
        result["evidence"] = _signed(result["evidence"])
        return result

    def identity_sign(params: dict) -> dict:
        """Sign a nonce chosen by the asker, so an address can prove it holds the key.

        The bytes are signed exactly as sent, and the answer names the key, which is what lets a
        party holding a card require the address to sign with the same key. One signed nonce per
        address and purpose per minute, because otherwise this is a signing oracle.
        """
        import time as _time

        nonce = params.get("nonce")
        if not isinstance(nonce, str) or len(nonce) < MIN_NONCE_CHARS:
            raise ValueError(
                f"nonce_too_short: a nonce of at least {MIN_NONCE_CHARS} characters is required"
            )
        purpose = params.get("purpose")
        if not isinstance(purpose, str) or not purpose.strip():
            raise ValueError("purpose_required: name what the signature is for")
        if not state.private_pem:
            raise ValueError("internal: this agent has no private key to sign with")

        window = (state.peer, purpose.strip().lower())
        now = _time.time()
        last = state.identity_requests.get(window)
        if last is not None and now - last < IDENTITY_WINDOW_SECONDS:
            raise ValueError(
                f"rate_limited: one signed nonce per address and purpose per "
                f"{IDENTITY_WINDOW_SECONDS}s"
            )
        state.identity_requests[window] = now

        signature = sign_bytes(state.private_pem, state.kid or state.name, nonce.encode("utf-8"))
        return {
            "signature": signature["value"],
            "kid": signature["kid"],
            "alg": signature["alg"],
            "purpose": purpose.strip(),
            "signed_bytes": len(nonce.encode("utf-8")),
        }

    def opportunity_decide(params: dict) -> dict:
        negotiation_id = params.get("negotiation_id")
        if negotiation_id not in state.opportunities:
            raise KeyError(f"unknown opportunity: {negotiation_id}")
        opportunity = state.opportunities[negotiation_id]
        status = opportunity.decide(params["actor"], bool(params["approve"]))
        return {
            "opportunity_id": negotiation_id,
            "status": status,
            "human_consent": opportunity.human_consent,
        }

    return {
        "agent/card": card,
        "arli/capabilities": capabilities,
        "arli/commitments": commitments,
        "arli/match": match,
        "arli/psi/keys": psi_holder_keys,
        "arli/psi/commit": psi_commit,
        "arli/psi/sign": psi_sign,
        "arli/psi/finish": psi_finish,
        "arli/psi/record": psi_record,
        "arli/claims/status": claims_status,
        "arli/claims/issuers": claims_issuers,
        "arli/claims/declare": claims_declare,
        "arli/registry/status": registry_status,
        "arli/registry/register": registry_register,
        "arli/registry/rotate": registry_rotate,
        "arli/registry/revoke": registry_revoke,
        "arli/registry/confirm": registry_confirm,
        "arli/registry/check": registry_check,
        "arli/retention/status": retention_status,
        "arli/retention/record": retention_record,
        "arli/retention/archive": retention_archive,
        "arli/retention/delete": retention_delete,
        "arli/retention/purge": retention_purge,
        "arli/negotiation/open": negotiation_open,
        "arli/negotiation/turn": negotiation_turn,
        "arli/negotiation/witness": negotiation_witness,
        "arli/identity/sign": identity_sign,
        "arli/opportunity/decide": opportunity_decide,
    }


def make_handler(state: AgentState):
    methods = _methods(state)
    # One surface for every binding: a mismatch with the shared list breaks startup.
    missing = [m for m in METHOD_NAMES if m not in methods]
    extra = [m for m in methods if m not in METHOD_NAMES]
    if missing or extra:
        raise RuntimeError(f"surface mismatch: missing={missing} extra={extra}")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args, **kwargs):  # silence in tests
            return

        def _send(self, code: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == CARD_PATH:
                self._send(200, state.card)
            elif self.path == CAPS_PATH:
                self._send(200, capability_document(state.operations))
            elif self.path == HEALTH_PATH:
                self._send(200, health_document(state))
            else:
                self._send(404, {"error": "not found", "path": self.path})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != RPC_PATH:
                self._send(404, {"error": "not found", "path": self.path})
                return
            length = int(self.headers.get("Content-Length", "0"))
            try:
                request = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError as exc:
                self._send(400, _rpc_err(None, -32700, f"parse error: {exc}"))
                return

            req_id = request.get("id")
            method = request.get("method")
            handler = methods.get(method)
            if handler is None:
                self._send(404, _rpc_err(req_id, -32601, f"unknown method: {method}"))
                return
            # The asking address bounds the identity method's rate window. Behind a proxy the
            # socket address is the proxy, so every visitor would share one window and a stranger
            # could block yours. The forwarded header names the caller. It is trustworthy only
            # behind a proxy that sets it, which is why the socket address is the fallback rather
            # than something we invent.
            forwarded = (self.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
            state.peer = forwarded or (self.client_address[0] if self.client_address else "")
            try:
                self._send(200, _rpc_ok(req_id, handler(request.get("params") or {})))
            except NegotiationError as exc:
                self._send(200, _rpc_err(req_id, -32001, f"grammar: {exc}"))
            except MatchRejected as exc:
                # A matching refusal carries its own code; it is not an internal failure.
                self._send(200, _rpc_err(req_id, -32002, f"{exc.code}: {exc.detail}"))
            except (ValueError, KeyError) as exc:
                message = str(exc)
                # A missing field is a refusal about the call, and it must carry a code like any
                # other refusal: a caller has to tell it apart from a failure of the engine.
                already_coded = any(code in message for code in CALL_REJECTIONS)
                if not already_coded and (
                    re.search(r"\b(is|are) required\b", message) or "missing required" in message
                ):
                    message = f"missing_field: {message}"
                elif isinstance(exc, KeyError):
                    message = f"missing_field: unknown key {message}"
                self._send(200, _rpc_err(req_id, -32602, message))
            except Exception as exc:  # noqa: BLE001
                self._send(200, _rpc_err(req_id, -32603, f"internal: {exc}"))

    return Handler


def start_agent(state: AgentState, port: int, host: str = "127.0.0.1") -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Start the agent in the background, returning the server and the thread.

    The bind address is a parameter because a container has to bind 0.0.0.0 while everything local
    should stay on the loopback. TLS and the public name belong in front, not here.
    """
    server = ThreadingHTTPServer((host, port), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
