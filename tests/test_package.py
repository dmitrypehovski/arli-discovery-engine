"""Упаковка: версии в одном месте, точки входа на месте, история честная.

Две версии не должны смешиваться. Версия пакета это код, версия расширения это текст
обещания. Если они совпадут случайно, это не достижение, а совпадение; если разъедутся по
ошибке в одной из них, тест обязан это показать.
"""

from __future__ import annotations

import importlib
import re
import tomllib
from pathlib import Path

import arli_engine

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_package_version_agrees_with_its_own_declaration():
    declared = _pyproject()["project"]["version"]
    assert declared == arli_engine.__version__


def test_the_extension_uri_is_well_formed():
    """Согласованность с самим текстом проверяет tests/test_publishing.py в доме текста."""
    assert arli_engine.EXTENSION_URI.endswith("arli.discovery.v1")
    assert arli_engine.EXTENSION_URI.startswith("https://arliworld".replace("world", "work"))


def test_the_two_versions_stay_distinguishable():
    """Версия кода и версия текста это разные вещи, и это должно быть видно."""
    assert arli_engine.__version__ != arli_engine.EXTENSION_VERSION


def test_entry_points_exist_and_point_at_callables():
    scripts = _pyproject()["project"]["scripts"]
    expected = {
        "arli-discovery-conformance": "arli_engine.conformance.__main__:main",
        "arli-discovery-client": "arli_engine.client.cli:main",
        "arli-discovery-mcp": "arli_engine.adapters.mcp.server:main",
        "arli-discovery-agent": "arli_engine.serve:main",
    }
    assert scripts == expected
    for target in scripts.values():
        module_name, attribute = target.split(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute)), target


def test_the_plugin_manifest_ships_with_the_package():
    data = _pyproject()["tool"]["setuptools"]["package-data"]
    assert data["arli_engine.adapters.plugin"] == ["*.json"]
    assert (ROOT / "arli_engine" / "adapters" / "plugin" / "arli-discovery.json").is_file()


def test_the_changelog_states_both_versions_and_the_limits():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "## 0.1.0" in changelog
    assert "not published" in changelog
    # ограничения версии объявляются, а не умалчиваются
    assert "Online probing of a single guess is not closed" in changelog
    assert "trust anchor" in changelog
    assert "Plugin manifest" in changelog or "plugin manifest" in changelog


def test_the_changelog_does_not_claim_extension_moves_with_code():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(r"they do not match today", changelog)
