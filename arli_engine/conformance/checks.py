"""A conformance suite for our layer: the thing that makes us the source of truth.

The value sets below are normative expectations. They are held by the SUITE, not by
the implementation: that is exactly why another party can get an answer of
"conformant" or an exact list of mismatches, rather than a retelling of someone
else's documentation.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field

import httpx
from cryptography.hazmat.primitives import serialization

from arli_engine.core.crypto import verify_envelope
from arli_engine.core.evidence import Evidence
from arli_engine.core.psi import feature_hash
from arli_engine.core.signing import jwk_to_public_key, verify_card_self

CARD_PATH = "/.well-known/agent-card.json"
CAPS_PATH = "/.well-known/arli-capabilities.json"
RPC_PATH = "/a2a/rpc"

# The suite's normative expectations.
EXPECTED_SCHEME = "closed_blind_rsa"
EXPECTED_MAX_TURNS = 12
EXPECTED_ACTIONS = ["propose", "counter", "accept", "decline"]
EXPECTED_TURN_REJECTIONS = [
    "not_a_seat",
    "unknown_action",
    "already_settled",
    "turn_limit",
    "not_your_turn",
    "first_turn_is_propose_or_decline",
    "seats_must_alternate",
    "accept_without_offer",
    "propose_over_offer",
    "not_a_witness",
    "witness_without_proof",
    "witness_card_unverified",
    "witness_seat_mismatch",
    "witness_proof_invalid",
    "witness_mismatch",
]

# The decision call is a different method from a turn, so its refusals are their own closed list (6.17).
EXPECTED_DECISION_REJECTIONS = ["not_agreed", "not_a_seat"]
EXPECTED_OPENING_REJECTIONS = [
    "intent_inactive",
    "synthetic_not_admitted",
    "same_owner",
    "same_intent",
    "network_mismatch",
    "types_not_comparable",
    "below_required_level",
]
EXPECTED_MATCH_REJECTIONS = [
    "too_many_features",
    "missing_pair_key",
    "missing_peer_key",
    "missing_private_key",
    "length_mismatch",
    "missing_floor",
    "invalid_floor",
]
EXPECTED_FLOOR = 4000
EXPECTED_EFFECT_KEYS = 7
EXPECTED_FEATURE_CAP = 64


@dataclass
class Target:
    """One implementation under test. It talks over HTTP, like another party would."""

    base_url: str
    name: str = "target"
    timeout: float = 20.0
    _client: "httpx.Client | None" = field(init=False, repr=False, default=None)

    def __post_init__(self) -> None:
        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            raise RuntimeError("this target is closed")
        return self._client

    def card(self) -> dict:
        return self.client.get(CARD_PATH).json()

    def capabilities(self) -> dict:
        return self.client.get(CAPS_PATH).json()

    def rpc(self, method: str, params: dict | None = None) -> dict:
        return self.client.post(
            RPC_PATH,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        ).json()

    def result(self, method: str, params: dict | None = None) -> dict:
        payload = self.rpc(method, params)
        if "error" in payload:
            raise AssertionError(f"{method} failed: {payload['error']['message']}")
        return payload["result"]

    def error_message(self, method: str, params: dict | None = None) -> str:
        payload = self.rpc(method, params)
        if "error" not in payload:
            raise AssertionError(f"{method} unexpectedly succeeded")
        return str(payload["error"].get("message", ""))

    def close(self) -> None:
        self._client.close()


@dataclass
class Report:
    target: str
    checks: list[dict] = field(default_factory=list)

    @property
    def failures(self) -> list[dict]:
        return [c for c in self.checks if not c["passed"]]

    @property
    def compliant(self) -> bool:
        return not self.failures

    def to_json(self) -> dict:
        return {
            "target": self.target,
            "compliant": self.compliant,
            "checks": self.checks,
            "failures": self.failures,
        }


def _check(report: Report, code: str, fn) -> None:
    """One run of one check: an exception inside a check is a failure, not a suite crash."""
    try:
        passed, detail = fn()
    except Exception as exc:  # noqa: BLE001
        passed, detail = False, f"{type(exc).__name__}: {exc}"
    report.checks.append({"code": code, "passed": bool(passed), "detail": str(detail)})


def run_conformance(target: Target, holder: Target | None = None) -> Report:
    """Check an implementation. Closed intersection needs two sides."""
    report = Report(target=target.name)
    holder = holder or target

    card_cache: dict = {}

    def card():
        if "card" not in card_cache:
            card_cache["card"] = target.card()
        return card_cache["card"]

    def agent_name() -> str:
        """Take the agent name from its card: the report label is not a negotiation name."""
        try:
            return card().get("name") or target.name
        except Exception:  # noqa: BLE001
            return target.name

    _check(report, "card.served", lambda: (
        all(card().get(k) for k in ("name", "version", "interfaces")),
        "card fields present" if card().get("interfaces") else "missing name/version/interfaces",
    ))

    _check(report, "card.self_verified", lambda: verify_card_self(card()))

    def tamper_detected():
        broken = {**card(), "description": (card().get("description") or "") + " (edited)"}
        ok, reasons = verify_card_self(broken)
        return (ok is False), reasons or "tampering went unnoticed"

    _check(report, "card.tamper_detected", tamper_detected)

    def caps_served():
        caps = target.capabilities()
        manifest = caps.get("manifest", {})
        return (manifest.get("fail_closed") is True and bool(manifest.get("operations"))), (
            "manifest present" if manifest.get("operations") else "no operations in manifest"
        )

    _check(report, "caps.served", caps_served)

    def caps_effects():
        caps = target.capabilities()
        bad = []
        for op in caps["manifest"]["operations"]:
            declared = op.get("declared_effect_keys") or []
            if len(declared) != EXPECTED_EFFECT_KEYS:
                if op.get("declaration") != "partial_by_design":
                    bad.append(op.get("name"))
        return (not bad), f"operations with incomplete effect declaration: {bad}"

    _check(report, "caps.effects_complete", caps_effects)

    def caps_unavailable():
        caps = target.capabilities()
        bad = [o.get("name") for o in caps["manifest"]["operations"]
               if not o.get("available") and not o.get("availability_reason")]
        return (not bad), f"unavailable operations without a reason: {bad}"

    _check(report, "caps.unavailable_reason", caps_unavailable)

    def caps_rules():
        rules = target.capabilities().get("negotiation_rules")
        if not isinstance(rules, dict):
            return False, "no negotiation_rules section"
        problems = []
        if rules.get("max_turns") != EXPECTED_MAX_TURNS:
            problems.append(f"max_turns={rules.get('max_turns')}")
        if rules.get("actions") != EXPECTED_ACTIONS:
            problems.append(f"actions={rules.get('actions')}")
        if rules.get("turn_rejections") != EXPECTED_TURN_REJECTIONS:
            problems.append(f"turn_rejections={rules.get('turn_rejections')}")
        if rules.get("decision_rejections") != EXPECTED_DECISION_REJECTIONS:
            problems.append(f"decision_rejections={rules.get('decision_rejections')}")
        if rules.get("opening_rejections") != EXPECTED_OPENING_REJECTIONS:
            problems.append(f"opening_rejections={rules.get('opening_rejections')}")
        return (not problems), f"closed lists differ: {problems}"

    _check(report, "caps.negotiation_rules", caps_rules)

    def caps_intersection():
        section = target.capabilities().get("intersection")
        if not isinstance(section, dict):
            return False, "no intersection section"
        problems = []
        if section.get("scheme") != EXPECTED_SCHEME:
            problems.append(f"scheme={section.get('scheme')}")
        if section.get("feature_cap") != EXPECTED_FEATURE_CAP:
            problems.append(f"feature_cap={section.get('feature_cap')}")
        for key in ("closed", "not_closed"):
            if not section.get(key):
                problems.append(f"missing {key}")
        if section.get("match_rejections") != EXPECTED_MATCH_REJECTIONS:
            problems.append("match_rejections differ")
        return (not problems), f"intersection section differs: {problems}"

    _check(report, "caps.intersection_section", caps_intersection)

    def not_a_seat():
        target.result("arli/negotiation/open", {
            "negotiation_id": "conf-seat", "peers": [agent_name(), "counterparty"],
        })
        message = target.error_message("arli/negotiation/turn", {
            "negotiation_id": "conf-seat", "seat": "somebody-else",
            "action": "propose", "message": "x",
        })
        return ("not_a_seat" in message), message or "a foreign seat took a turn"

    _check(report, "negotiation.not_a_seat", not_a_seat)

    def first_turn_grammar():
        """The first turn is a proposal or a refusal: acceptance without a proposal fails.

        The check is one-sided on purpose: across two separate implementations one
        cannot stage the other side's proposal, so here we check the first-turn rule,
        while the accept_without_offer case itself is covered by core tests with a
        shared log.
        """
        session = "conf-accept"
        target.result("arli/negotiation/open", {
            "negotiation_id": session, "peers": [agent_name(), "counterparty"],
        })
        message = target.error_message("arli/negotiation/turn", {
            "negotiation_id": session, "seat": agent_name(), "action": "accept", "message": "x",
        })
        return ("first_turn_is_propose_or_decline" in message), message or "accept was allowed as the first turn"

    _check(report, "negotiation.first_turn_grammar", first_turn_grammar)

    def _open_with(standings, **extra):
        params = {"negotiation_id": "conf-intent", "peers": [agent_name(), "counterparty"],
                  "network": "conf", "intents": standings}
        params.update(extra)
        return target.result("arli/negotiation/open", params)

    def supply_demand_comparable():
        result = _open_with([
            {"intent_id": "i1", "owner": agent_name(), "network": "conf",
             "type": "supply", "statement": "spare line"},
            {"intent_id": "i2", "owner": "counterparty", "network": "conf",
             "type": "demand", "statement": "needs capacity"},
        ])
        return (result.get("opened") is True), f"supply with demand: {result.get('opened')}"

    _check(report, "intent.supply_demand_comparable", supply_demand_comparable)

    def same_type_refused():
        result = _open_with([
            {"intent_id": "i1", "owner": agent_name(), "network": "conf", "type": "supply"},
            {"intent_id": "i2", "owner": "counterparty", "network": "conf", "type": "supply"},
        ])
        return (
            result.get("opened") is False and result.get("rejection") == "types_not_comparable",
            f"two supplies: {result.get('opened')} {result.get('rejection')}",
        )

    _check(report, "intent.same_type_refused", same_type_refused)

    def synthetic_refused():
        result = _open_with([
            {"intent_id": "i1", "owner": agent_name(), "network": "conf",
             "type": "person", "provenance": "synthetic"},
            {"intent_id": "i2", "owner": "counterparty", "network": "conf", "type": "person"},
        ])
        return (
            result.get("opened") is False and result.get("rejection") == "synthetic_not_admitted",
            f"synthetic in a human network: {result.get('opened')} {result.get('rejection')}",
        )

    _check(report, "intent.synthetic_refused", synthetic_refused)

    def refusal_evidence():
        message = target.result("arli/negotiation/open", {
            "negotiation_id": "conf-refusal", "peers": [agent_name(), "counterparty"],
            "network": "conf", "intents": [
                {"intent_id": "i1", "owner": agent_name(), "network": "conf", "active": False},
                {"intent_id": "i2", "owner": "counterparty", "network": "conf"},
            ],
        })
        if message.get("opened") is not False:
            return False, "an inactive intent must not open a pair"
        envelope = message.get("evidence") or {}
        if not Evidence.from_json(envelope).verify_id():
            return False, "refusal carries no reproducible evidence id"
        return True, message.get("rejection", "")

    _check(report, "evidence.on_refusal", refusal_evidence)

    def evidence_signed():
        envelope = target.result("arli/negotiation/open", {
            "negotiation_id": "conf-signed", "peers": [agent_name(), "counterparty"],
            "network": "conf", "intents": [
                {"intent_id": "i1", "owner": agent_name(), "network": "conf", "active": False},
                {"intent_id": "i2", "owner": "counterparty", "network": "conf"},
            ],
        }).get("evidence") or {}
        if not envelope.get("signature"):
            return False, "envelope is not signed"
        ok, reasons = verify_envelope(envelope, _pem(_agent_public_key(card())))
        return ok, reasons or f"signed by kid {envelope['signature'].get('kid')}"

    _check(report, "evidence.signed", evidence_signed)

    closed = {}

    def closed_flow():
        import secrets

        session_id = secrets.token_hex(6)
        keys = holder.result("arli/psi/keys", {"session_id": session_id})
        commit = target.result("arli/psi/commit", {
            "session_id": session_id, "peer_name": holder.name,
            "n": keys["n"], "e": keys["e"],
        })
        signed = holder.result("arli/psi/sign", {
            "session_id": session_id, "blinded": commit["blinded"],
        })
        match_a = target.result("arli/psi/finish", {
            "session_id": session_id, "n": keys["n"],
            "signed": signed["signed"], "holder_tokens": signed["holder_tokens"],
            "floor_basis_points": EXPECTED_FLOOR,
        })
        match_b = holder.result("arli/psi/record", {
            "session_id": session_id, "matched": match_a["matched_features"],
            "floor_basis_points": EXPECTED_FLOOR,
        })
        closed.update({
            "keys": keys, "commit": commit, "signed": signed,
            "match_a": match_a, "match_b": match_b, "session_id": session_id,
        })
        return True, f"session {session_id}"

    _check(report, "match.closed_flow_runs", closed_flow)

    def symmetric():
        """Both sides learn the same intersection, each in its own spelling of the shared item.

        With the vocabulary, a need and an offer of the same commodity are one token but two strings, so
        comparing the strings for equality would fail a correct implementation. The intersection is
        compared as tokens; the strings are printed for a reader.
        """
        from arli_engine.core.vocabulary import canonical_token

        a = closed.get("match_a", {}).get("matched_features") or []
        b = closed.get("match_b", {}).get("matched_features") or []
        same = sorted(canonical_token(item) for item in a) == sorted(canonical_token(item) for item in b)
        return same, f"A={a} B={b} as tokens: {'equal' if same else 'different'}"

    _check(report, "match.symmetric", symmetric)

    def blinded_only():
        blob = " ".join(closed.get("commit", {}).get("blinded", []))
        leaked = [f for f in ("role:", "skill:", "city:") if f in blob]
        return (not leaked), f"feature-looking strings in the outbound payload: {leaked}"

    _check(report, "match.blinded_only", blinded_only)

    def offline_enumeration():
        tokens = {int(v, 16) for v in closed.get("signed", {}).get("holder_tokens", [])}
        keys = closed.get("keys", {})
        if not tokens or not keys:
            return False, "closed flow did not produce tokens"
        from arli_engine.core.psi import feature_hash

        n, e = int(keys["n"], 16), int(keys["e"], 16)
        hits = [g for g in ("role:founder", "skill:design", "city:sf", "role:investor")
                if pow(feature_hash(g), e, n) in tokens]
        return (not hits), f"guesses matching holder tokens: {hits}"

    _check(report, "match.offline_enumeration_closed", offline_enumeration)

    _check(report, "match.floor_declared", lambda: (
        "missing_floor" in target.error_message("arli/psi/finish", {
            "session_id": closed.get("session_id", "none"),
            "n": closed.get("keys", {}).get("n", "0"),
            "signed": closed.get("signed", {}).get("signed", []),
            "holder_tokens": closed.get("signed", {}).get("holder_tokens", []),
        }),
        "a match without a declared floor must be refused",
    ))

    def below_floor():
        import secrets

        session_id = secrets.token_hex(6)
        keys = holder.result("arli/psi/keys", {"session_id": session_id})
        commit = target.result("arli/psi/commit", {
            "session_id": session_id, "peer_name": holder.name,
            "n": keys["n"], "e": keys["e"],
        })
        signed = holder.result("arli/psi/sign", {
            "session_id": session_id, "blinded": commit["blinded"],
        })
        high = target.result("arli/psi/finish", {
            "session_id": session_id, "n": keys["n"],
            "signed": signed["signed"], "holder_tokens": signed["holder_tokens"],
            "floor_basis_points": 10000,
        })
        parameters = high["evidence"]["parameters"]
        return (
            high["matched_features"] == [] and parameters.get("passed_floor") is False,
            f"matched={high['matched_features']} passed_floor={parameters.get('passed_floor')}",
        )

    _check(report, "match.below_floor_is_empty", below_floor)

    _check(report, "match.feature_cap", lambda: (
        "too_many_features" in holder.error_message("arli/psi/sign", {
            "session_id": closed.get("session_id", "none"),
            "blinded": ["1"] * (EXPECTED_FEATURE_CAP + 1),
        }),
        "above the cap the exchange must be refused, not truncated",
    ))

    _check(report, "match.no_counterparty_count", lambda: (
        all(
            "counterpart_feature_count" not in (closed.get(side, {}).get("evidence", {}).get("parameters") or {})
            for side in ("match_a", "match_b")
        ),
        "the count of the other side must not be disclosed",
    ))

    def registry_rotation_separate_from_revocation():
        """Rotation closes a key; principal revocation blocks new keys."""
        import secrets as _secrets

        suffix = _secrets.token_hex(3)
        kid, new_kid = f"conf-key-{suffix}", f"conf-key2-{suffix}"
        principal = f"conf-principal-{suffix}"
        target.result("arli/registry/register", {
            "kid": kid, "principal_id": principal, "public_pem": f"pem-{suffix}",
        })
        target.result("arli/registry/rotate", {
            "kid": kid, "new_kid": new_kid, "new_public_pem": f"pem2-{suffix}",
        })
        old_ok = target.result("arli/registry/check", {"kid": kid})
        new_ok = target.result("arli/registry/check", {"kid": new_kid})
        target.result("arli/registry/revoke", {"principal_id": principal, "reason": "policy"})
        after = target.result("arli/registry/check", {"kid": new_kid})
        ok = (
            old_ok["valid"] is False
            and new_ok["valid"] is True
            and after["valid"] is False
            and "principal_revoked" in after["reasons"]
        )
        return ok, f"old={old_ok['reasons']} new={new_ok['valid']} after={after['reasons']}"

    _check(report, "registry.rotation_separate_from_revocation", registry_rotation_separate_from_revocation)

    def retention_hides_archived_envelopes():
        import secrets as _secrets

        suffix = _secrets.token_hex(3)
        evidence_id, intent_id = f"conf-ev-{suffix}", f"conf-intent-{suffix}"
        target.result("arli/retention/record", {"evidence_id": evidence_id, "intent_id": intent_id})
        archived = target.result("arli/retention/archive", {"intent_id": intent_id})
        status = target.result("arli/retention/status", {})["retention"]
        record = [r for r in status["records"] if r["evidence_id"] == evidence_id]
        ok = (
            archived["marked_deleted"] == 1
            and evidence_id not in archived["searchable"]
            and record
            and record[0]["visible"] is False
            and record[0]["deleted_reason"] == "intent_archived"
        )
        return ok, f"marked={archived['marked_deleted']} record={record}"

    _check(report, "retention.archived_is_hidden", retention_hides_archived_envelopes)

    def bindings_expose_the_same_surface():
        """Both bindings must describe one surface, not their own lists."""
        bindings = target.capabilities().get("bindings") or {}
        a2a_methods = list(bindings.get("a2a", {}).get("methods") or [])
        mcp_tools = list(bindings.get("mcp", {}).get("tools") or [])
        plugin_tools = list(bindings.get("plugin", {}).get("tools") or [])
        expected = [method.replace("/", "_") for method in a2a_methods]
        ok = (
            bool(a2a_methods)
            and mcp_tools == expected
            and plugin_tools == expected
            and len(set(mcp_tools)) == len(mcp_tools)
        )
        return ok, (
            f"methods={len(a2a_methods)} mcp={len(mcp_tools)} plugin={len(plugin_tools)} "
            f"all_agree={mcp_tools == expected and plugin_tools == expected}"
        )

    _check(report, "surface.bindings_agree", bindings_expose_the_same_surface)

    def claims_level_needs_an_issuer():
        """A level above declared without an issuer envelope is not accepted."""
        import secrets as _secrets

        suffix = _secrets.token_hex(3)
        # Two cases: an issuer is named without its envelope, and no issuer is named at all.
        named = target.result("arli/claims/declare", {
            "feature": f"skill:{suffix}", "level": "third_party_verified",
            "issuer": f"body-{suffix}",
        })
        unnamed = target.result("arli/claims/declare", {
            "feature": f"doc:{suffix}", "level": "topic_owner_verified",
        })
        ok = (
            named["verified"] is False
            and "level_upgraded_without_issuer" in named["reasons"]
            and named["claim"]["has_issuer_evidence"] is False
            and unnamed["verified"] is False
            and "missing_issuer" in unnamed["reasons"]
        )
        return ok, f"named={named['reasons']} unnamed={unnamed['reasons']}"

    _check(report, "claims.level_needs_issuer", claims_level_needs_an_issuer)

    def claims_required_level_is_enforced():
        """A network that required a verified feature does not open a pair on a declared one."""
        import secrets as _secrets

        suffix = _secrets.token_hex(3)
        base = {
            "peers": [agent_name(), "counterparty"], "network": "conf",
            "intents": [
                {"intent_id": "i1", "owner": agent_name(), "network": "conf", "type": "person"},
                {"intent_id": "i2", "owner": "counterparty", "network": "conf", "type": "person"},
            ],
        }
        weak = dict(base, negotiation_id=f"claim-weak-{suffix}", required_level="third_party_verified")
        weak_result = target.result("arli/negotiation/open", weak)

        strong_intents = [
            {"intent_id": "i1", "owner": agent_name(), "network": "conf", "type": "person",
             "claim_level": "topic_owner_verified"},
            {"intent_id": "i2", "owner": "counterparty", "network": "conf", "type": "person",
             "claim_level": "third_party_verified"},
        ]
        strong = dict(base, negotiation_id=f"claim-strong-{suffix}",
                      required_level="third_party_verified", intents=strong_intents)
        strong_result = target.result("arli/negotiation/open", strong)

        ok = (
            weak_result.get("opened") is False
            and weak_result.get("rejection") == "below_required_level"
            and strong_result.get("opened") is True
        )
        return ok, f"weak={weak_result.get('rejection')} strong_opened={strong_result.get('opened')}"

    _check(report, "claims.required_level_is_enforced", claims_required_level_is_enforced)

    def missing_field_carries_a_code():
        """A refusal about the call itself must carry a code, like any other refusal."""
        response = httpx.post(f"{target.base_url}{RPC_PATH}", json={
            "jsonrpc": "2.0", "id": 1, "method": "arli/registry/check", "params": {},
        }, timeout=10)
        payload = response.json()
        message = (payload.get("error") or {}).get("message", "")
        return "missing_field" in message, f"message={message!r}"

    _check(report, "call.missing_field_carries_a_code", missing_field_carries_a_code)

    def call_refusals_are_published():
        codes = target.capabilities().get("call_rejections") or []
        ok = "missing_field" in codes and "unknown_method" in codes and len(codes) >= 4
        return ok, f"call_rejections={codes}"

    _check(report, "caps.call_refusals_published", call_refusals_are_published)

    return report


def _pem(key) -> bytes:
    return key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _agent_public_key(card: dict):
    """Take the agent key from its own card: another party has no other key."""
    protected = (card.get("signatures") or [{}])[0].get("protected", "")
    if not protected:
        raise ValueError("card carries no signature to take the key from")
    padded = protected + "=" * (-len(protected) % 4)
    header = json.loads(base64.urlsafe_b64decode(padded))
    jwk = header.get("jwk")
    if not isinstance(jwk, dict):
        raise ValueError("card header carries no jwk to take the key from")
    return jwk_to_public_key(jwk)
