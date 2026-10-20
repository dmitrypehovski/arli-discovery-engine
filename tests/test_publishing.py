"""Packaging for publication on the engine side: license, mark, a separate home for the text.

We check what cannot be checked by eye once and then forgotten. The license text must match
the official Apache-2.0 byte for byte: a swapped or shortened license is not a trifle but a
broken permission. The normative text must live SEPARATELY from the package with the code:
the engine references it and keeps no copy of its own. Otherwise the copy would quietly
drift from the text, and we would do one thing while promising another.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The official Apache License 2.0 text from the Apache Software Foundation site.
CANONICAL_LICENSE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"

CYRILLIC = re.compile(r"[\u0410-\u044f\u0401\u0451]")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def spec_dir() -> Path:
    """The home of the normative text. A move is set by an environment variable, not by editing the test."""
    override = os.environ.get("ARLI_SPEC_DIR")
    return Path(override) if override else ROOT.parent / "arli-spec"


def require_spec_house() -> Path:
    """The normative text lives in its own home. In a clean clone it is not adjacent.

    This is not a reason to silently skip the check: the reason is named plainly, so the
    absence of the text does not look like a green run.
    """
    house = spec_dir()
    if not (house / "SPEC.en.md").is_file():
        pytest.skip(f"the normative text is not adjacent ({house}); set ARLI_SPEC_DIR to check it")
    return house


def spec_file(name: str) -> str:
    return (spec_dir() / name).read_text(encoding="utf-8")


def test_license_is_the_unmodified_apache_text():
    text = _read("LICENSE")
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert digest == CANONICAL_LICENSE_SHA256, "LICENSE is not the official Apache-2.0 text"
    assert "Apache License" in text
    assert "Version 2.0, January 2004" in text


def test_notice_states_the_holder_and_reserves_the_mark():
    notice = _read("NOTICE")
    assert "Shenzhen Capernaum Technology Co., Ltd." in notice
    assert "Apache License, Version 2.0" in notice
    assert "NOT licensed under" in notice
    assert "TRADEMARK.md" in notice


def test_trademark_policy_reserves_the_mark_and_points_at_certification():
    policy = _read("TRADEMARK.md")
    assert "ARLI verified" in policy
    assert "remain the property of Shenzhen Capernaum Technology Co., Ltd." in policy
    # the mark is not inherited from the license: this must be said plainly
    assert "not granted by the" in policy
    assert "conformance suite v" in policy


def test_contributing_terms_are_inbound_equals_outbound():
    terms = _read("CONTRIBUTING.md")
    assert "Inbound equals outbound" in terms
    assert "perpetual, worldwide, non-exclusive, royalty-free" in terms
    assert "git commit -s" in terms


def test_the_normative_text_lives_outside_the_engine():
    """The promise text must live separately: the engine references it and keeps no copy."""
    require_spec_house()
    assert not (ROOT / "spec").exists(), "the engine must not keep a copy of the normative text"


def test_the_engine_matches_the_version_of_the_text_it_depends_on():
    import arli_engine

    require_spec_house()
    english = spec_file("SPEC.en.md")
    assert f"# ARLI Discovery Extension v{arli_engine.EXTENSION_VERSION}" in english
    assert arli_engine.EXTENSION_URI in english


def test_changing_the_spec_house_is_a_setting_not_a_code_edit():
    """A move is set by an environment variable, so the separation is not broken by editing tests."""
    assert os.environ.get("ARLI_SPEC_DIR") is None or Path(os.environ["ARLI_SPEC_DIR"]).is_dir()


def test_the_engine_does_not_publish_the_text():
    """The text must not be in the wheel: it is published separately from the code."""
    language = _read("pyproject.toml")
    assert "arli-spec" not in language or "SPEC.en.md" not in language
    assert not (ROOT / "docs" / "spec.html").exists(), "the spec page belongs to the spec house"


def test_package_metadata_is_publishable():
    """Package metadata goes outward together with the code, so it carries no Cyrillic."""
    metadata = _read("pyproject.toml")
    assert 'license = "Apache-2.0"' in metadata
    assert 'license-files = ["LICENSE", "NOTICE"]' in metadata
    description = re.search(r'^description = "(.+)"$', metadata, re.M)
    assert description is not None
    assert not CYRILLIC.search(description.group(1))


def test_readme_points_at_the_suite_the_licence_and_the_honest_limits():
    readme = _read("README.md")
    assert "arli_engine.conformance" in readme
    assert "Apache License 2.0" in readme
    # the code is out: both addresses must be named, and the stale claim about Russian notes is gone
    assert "published under Apache-2.0" in readme
    assert "https://github.com/dmitrypehovski/arli-discovery-engine" in readme
    assert "comments in the code are" not in readme
    # limitations must be in the description, not only in the specification
    assert "does not close online probing" in readme
    assert "not a unique human" in readme
