"""A single list of surface methods: one point of truth for every binding.

The point of this file is to keep the adapters from drifting apart. The engine
surface is one, but there are several bindings to it: our HTTP-based A2A and MCP
for those who live in the world of model tools. If each binding kept its own list,
they would diverge on the first edit and diverge silently. So the list lives here,
and the bindings must match it and are checked for a match in the conformance suite.

Naming rule: an MCP tool name is the method name with the slash replaced by an
underscore. The rule is reversible, so the one-to-one correspondence is checkable.
"""

from __future__ import annotations

# Methods the agent must support per the specification. The order is fixed so that
# the capability document is byte-stable between runs.
METHOD_NAMES = (
    "agent/card",
    "arli/capabilities",
    "arli/commitments",
    "arli/match",
    "arli/psi/keys",
    "arli/psi/commit",
    "arli/psi/sign",
    "arli/psi/finish",
    "arli/psi/record",
    "arli/registry/status",
    "arli/registry/register",
    "arli/registry/rotate",
    "arli/registry/revoke",
    "arli/registry/confirm",
    "arli/registry/check",
    "arli/retention/status",
    "arli/retention/record",
    "arli/retention/archive",
    "arli/retention/delete",
    "arli/retention/purge",
    "arli/claims/status",
    "arli/claims/issuers",
    "arli/claims/declare",
    "arli/negotiation/open",
    "arli/negotiation/turn",
    "arli/opportunity/decide",
)

# Methods that change the agent's state. Needed so that the MCP tool description is
# honest: the caller sees that the action leaves a trace.
# Refusals about the call itself, as opposed to refusals about a pair, a claim or a match.
# They exist so that a caller can tell "you forgot a field" from "the engine failed".
CALL_REJECTIONS = (
    "missing_field",
    "unknown_method",
    "parse_error",
    "bad_type",
)

MUTATING_METHODS = (
    "arli/psi/commit",
    "arli/psi/sign",
    "arli/psi/finish",
    "arli/psi/record",
    "arli/registry/register",
    "arli/registry/rotate",
    "arli/registry/revoke",
    "arli/registry/confirm",
    "arli/retention/record",
    "arli/retention/archive",
    "arli/retention/delete",
    "arli/retention/purge",
    "arli/claims/issuers",
    "arli/claims/declare",
    "arli/negotiation/open",
    "arli/negotiation/turn",
    "arli/opportunity/decide",
)

# Required fields for methods where an empty call is meaningless. The list is
# deliberately incomplete: it honestly lists what is truly required and does not
# invent constraints where the engine imposes none.
REQUIRED_FIELDS = {
    "arli/commitments": ("session_id", "peer_name"),
    "arli/match": ("session_id", "peer_name", "counterpart_commitments"),
    "arli/psi/keys": (),
    "arli/psi/commit": ("features",),
    "arli/psi/sign": ("blinded",),
    "arli/psi/finish": ("signed", "floor_basis_points"),
    "arli/psi/record": ("matched", "floor_basis_points"),
    "arli/registry/register": ("kid", "principal_id", "public_pem"),
    "arli/registry/rotate": ("kid", "new_kid", "new_public_pem"),
    # revoke accepts either a key or a principal; the probe uses the key form
    "arli/registry/revoke": ("reason", "kid"),
    "arli/registry/confirm": ("principal_id",),
    "arli/registry/check": ("kid",),
    "arli/retention/record": ("evidence_id", "intent_id"),
    "arli/retention/archive": ("intent_id",),
    "arli/retention/delete": ("evidence_id",),
    "arli/claims/issuers": ("name", "public_pem"),
    "arli/claims/declare": ("feature", "level"),
    "arli/negotiation/open": ("negotiation_id", "peers", "intents"),
    "arli/negotiation/turn": ("negotiation_id", "seat", "action"),
    "arli/opportunity/decide": ("negotiation_id", "actor", "approve"),
}


def tool_name(method: str) -> str:
    """The MCP tool name for a method name."""
    return method.replace("/", "_")


# The reverse mapping table is built from the list, not from the substitution rule:
# the substitution rule is not always reversible, while the table is reversible by
# construction.
_TOOL_TO_METHOD = {tool_name(method): method for method in METHOD_NAMES}


def method_for_tool(name: str) -> str:
    """The method name for a tool name. Inverse of tool_name, exact or a refusal."""
    try:
        return _TOOL_TO_METHOD[name]
    except KeyError:
        raise ValueError(f"unknown tool: {name!r}")


def is_mutating(method: str) -> bool:
    return method in MUTATING_METHODS


def required_fields(method: str) -> list[str]:
    return list(REQUIRED_FIELDS.get(method, ()))
