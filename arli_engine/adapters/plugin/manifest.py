"""Plugin adapter: a tool manifest for installing into a user's environment.

The plugin brings no engine code of its own and does not reach the network directly.
It runs the same MCP binding as a separate process and declares its tools. So there is
almost no code here, just a manifest: name, version, launch command, tool list and
honest limitations.

The manifest is not written by hand. A hand-written manifest drifts from the engine on
the first edit, and drifts silently: an installed plugin would start calling a tool
that no longer exists. So the manifest is built from the shared surface list, and the
conformance suite checks that the written file matches the built one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from arli_engine.adapters.mcp.server import build_tools
from arli_engine.core.surface import METHOD_NAMES, tool_name

PLUGIN_NAME = "arli-discovery"
PLUGIN_VERSION = "0.1.0"
MANIFEST_FILENAME = "arli-discovery.json"

# The launch command: the same package as the MCP binding. The plugin does not duplicate the engine.
SERVER_COMMAND = (sys.executable, "-m", "arli_engine.adapters.mcp.server")

LIMITATIONS = [
    "The plugin runs the MCP binding as a local process; it holds no engine logic of its own.",
    "Features never leave the agent: the plugin cannot read raw feature values.",
    "Matching is not a search: a smaller intersection is a smaller answer, not a worse one.",
]


def build_manifest(*, command: tuple[str, ...] = SERVER_COMMAND) -> dict:
    """Build the manifest from the surface. One source, no hand-written lists."""
    tools = build_tools()
    return {
        "name": PLUGIN_NAME,
        "version": PLUGIN_VERSION,
        "description": (
            "ARLI discovery: an agent that matches on declared intent without an index holder. "
            "Ships the A2A surface to a model runtime as MCP tools."
        ),
        "transport": {"type": "stdio", "jsonrpc": "2.0", "framing": "newline-delimited"},
        "server": {"command": list(command), "env": {}},
        "tools": tools,
        "surface": {
            "methods": list(METHOD_NAMES),
            "tool_names": [tool_name(method) for method in METHOD_NAMES],
            "rule": "generated from the engine surface, never hand-written",
        },
        "limitations": list(LIMITATIONS),
    }


def manifest_json(*, command: tuple[str, ...] = SERVER_COMMAND) -> str:
    return json.dumps(build_manifest(command=command), ensure_ascii=False, indent=2) + "\n"


def manifest_path() -> Path:
    return Path(__file__).with_name(MANIFEST_FILENAME)


def write_manifest(path: Path | None = None) -> Path:
    target = path or manifest_path()
    target.write_text(manifest_json(), encoding="utf-8")
    return target
