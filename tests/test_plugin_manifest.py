"""The plugin manifest: built from the surface and not drifting from the engine.

We check what the manifest is generated from code for in the first place: the tool list
matches the method list, the launch command points at the same binding, and the file
recorded on disk matches the generated one. The last item is the guard against silent
drift.
"""

from __future__ import annotations

import json

from arli_engine.adapters.mcp.server import build_tools
from arli_engine.adapters.plugin.manifest import (
    LIMITATIONS,
    PLUGIN_NAME,
    SERVER_COMMAND,
    build_manifest,
    manifest_json,
    manifest_path,
)
from arli_engine.core.surface import METHOD_NAMES


def test_manifest_tools_match_the_surface_exactly():
    manifest = build_manifest()
    assert len(manifest["tools"]) == len(METHOD_NAMES)
    assert [tool["name"] for tool in manifest["tools"]] == [
        tool["name"] for tool in build_tools()
    ]
    assert manifest["surface"]["methods"] == list(METHOD_NAMES)
    assert manifest["surface"]["tool_names"] == [tool["name"] for tool in manifest["tools"]]


def test_manifest_summarises_itself_honestly():
    manifest = build_manifest()
    assert manifest["name"] == PLUGIN_NAME
    assert manifest["transport"]["type"] == "stdio"
    assert manifest["transport"]["framing"] == "newline-delimited"
    assert manifest["server"]["command"] == list(SERVER_COMMAND)
    # the MCP engine must be part of the launch command: the plugin does not duplicate logic
    assert "arli_engine.adapters.mcp.server" in manifest["server"]["command"]
    assert manifest["limitations"] == list(LIMITATIONS)
    assert any("no engine logic of its own" in note for note in manifest["limitations"])


def test_command_points_at_the_mcp_binding_that_actually_exists():
    import importlib

    module = importlib.import_module("arli_engine.adapters.mcp.server")
    assert hasattr(module, "McpServer")
    assert manifest_json() == json.dumps(build_manifest(), ensure_ascii=False, indent=2) + "\n"


def test_the_recorded_manifest_on_disk_matches_the_generated_one():
    """A mismatch means the file was hand-edited: that is a defect, not a convenience."""
    recorded = manifest_path().read_text(encoding="utf-8")
    assert recorded == manifest_json(), "manifest on disk drifted from the engine surface"


def test_recorded_manifest_is_valid_json_with_every_tool_documented():
    data = json.loads(manifest_path().read_text(encoding="utf-8"))
    for tool in data["tools"]:
        assert tool["name"] and tool["description"]
        assert tool["inputSchema"]["type"] == "object"
