"""A thin wrapper over the agent surface for external clients.

We keep one layer and many wrappers. The layer is the method list in `core/surface.py`,
and the wrappers are ways to reach it: A2A over HTTP, MCP over streams, the plugin. This
file is the fourth wrapper, for someone writing Python who does not want to assemble
requests by hand.

The wrapper does not describe methods anew, it unfolds the same list: one method per
surface method. If a method appears in the list, it appears here too, and if it
disappears, it does not remain here. A test checks this.

Refusals are not swallowed. The reason the engine returned as a code stays a code on the
client side too: `AgentRefused.code`. A client that shows "error 500" instead of a reason
forces people to figure out again what we already knew.
"""

from __future__ import annotations

from typing import Any, Callable

import httpx

from arli_engine.core.intent import INTENT_REJECTIONS
from arli_engine.core.match import MATCH_REJECTIONS
from arli_engine.core.negotiate import OPENING_REJECTIONS, REJECTIONS
from arli_engine.core.registry import REGISTRY_REJECTIONS
from arli_engine.core.retention import RETENTION_REJECTIONS
from arli_engine.core.surface import CALL_REJECTIONS, METHOD_NAMES

CARD_PATH = "/.well-known/agent-card.json"
CAPS_PATH = "/.well-known/arli-capabilities.json"
RPC_PATH = "/a2a/rpc"

# All engine refusal codes in one place: they are used to pull the reason out of an error text.
KNOWN_REJECTION_CODES: tuple[str, ...] = (
    tuple(CALL_REJECTIONS)
    + tuple(REJECTIONS)
    + tuple(OPENING_REJECTIONS)
    + tuple(MATCH_REJECTIONS)
    + tuple(RETENTION_REJECTIONS)
    + tuple(REGISTRY_REJECTIONS)
    + tuple(INTENT_REJECTIONS)
)


class AgentClientError(RuntimeError):
    """The surface replied in a way that cannot be parsed."""


class AgentRefused(RuntimeError):
    """The agent refused for a reason from a closed list."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        # Do not repeat a code the message already carries: "missing_field: missing_field: ..."
        # reads like two different problems.
        if code == "unlisted" or message.startswith(f"{code}:"):
            super().__init__(message)
        else:
            super().__init__(f"{code}: {message}")


def rejection_code(message: str) -> str:
    """Pull the refusal code out of the text. Without a code return "unlisted", not a guess."""
    for code in KNOWN_REJECTION_CODES:
        if code in message:
            return code
    return "unlisted"


class AgentClient:
    """A client for one agent. Surface methods are unfolded from the shared list."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=timeout, transport=transport)

    # --- plumbing ---
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AgentClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_card(self) -> dict:
        response = self._client.get(f"{self.base_url}{CARD_PATH}")
        response.raise_for_status()
        return response.json()

    def get_capabilities(self) -> dict:
        response = self._client.get(f"{self.base_url}{CAPS_PATH}")
        response.raise_for_status()
        return response.json()

    def call(self, method: str, params: dict | None = None) -> dict:
        """Call a method by name. A refusal reason arrives as AgentRefused."""
        if method not in METHOD_NAMES:
            raise ValueError(f"unknown method: {method!r}")
        response = self._client.post(
            f"{self.base_url}{RPC_PATH}",
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentClientError(f"{method} returned a non-JSON body: {exc}") from exc
        if "error" in payload:
            message = str(payload["error"].get("message", payload["error"]))
            raise AgentRefused(rejection_code(message), message)
        if "result" not in payload:
            raise AgentClientError(f"{method} returned neither result nor error: {payload}")
        return payload["result"]


def _bind(method: str) -> Callable[..., dict]:
    def call(self: AgentClient, params: dict | None = None) -> dict:
        return self.call(method, params)

    call.__name__ = method.replace("/", "_")
    call.__qualname__ = f"AgentClient.{call.__name__}"
    call.__doc__ = f"Call {method}. Arguments are passed to the engine unchanged."
    return call


# One per surface method. A mismatch is caught by a test.
def _install_bindings() -> None:
    for method in METHOD_NAMES:
        setattr(AgentClient, method.replace("/", "_"), _bind(method))


_install_bindings()


def bound_method_names() -> list[str]:
    """The names under which methods are available on the client, in list order."""
    return [method.replace("/", "_") for method in METHOD_NAMES]


def as_params(**kwargs: Any) -> dict:
    """A small convenience: assemble parameters from keyword arguments."""
    return dict(kwargs)
