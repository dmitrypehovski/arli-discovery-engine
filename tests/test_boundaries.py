"""The layer boundary: the core knows nothing about transport.

The rule from section 10 of the plan: there must not be a single import of network,
threads or HTTP anywhere under ``arli_engine/core``. It is enforced by a test, not by a
promise.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1] / "arli_engine" / "core"

# forbidden root modules: network, threads, HTTP
FORBIDDEN = {
    "http",
    "httpx",
    "requests",
    "urllib",
    "urllib3",
    "socket",
    "ssl",
    "threading",
    "asyncio",
    "multiprocessing",
    "socketserver",
}

CORE_FILES = sorted(CORE.glob("*.py"))


def _root_of(node: ast.Import | ast.ImportFrom) -> str | None:
    if isinstance(node, ast.Import):
        return node.names[0].name.split(".")[0]
    if node.module:
        return node.module.split(".")[0]
    return None


def test_core_has_files_to_check() -> None:
    """The test must not pass idly on an empty directory."""
    assert CORE_FILES, f"there are no files to check in {CORE}"


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_core_does_not_import_transport(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            root = _root_of(node)
            if root in FORBIDDEN:
                offenders.append(f"line {node.lineno}: {root}")
    assert not offenders, f"{path.name} imports transport: {offenders}"


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_core_imports_are_absolute_engine_paths(path: Path) -> None:
    """The core imports only itself, through an absolute package path."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            pytest.fail(f"{path.name} line {node.lineno}: relative import instead of absolute")
