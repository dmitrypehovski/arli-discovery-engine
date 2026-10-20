"""Упаковка к публикации на стороне движка: лицензия, знак, отдельный дом текста.

Проверяется то, что нельзя проверить глазами один раз и забыть. Текст лицензии обязан
совпадать с официальным Apache-2.0 побайтово: подменённая или сокращённая лицензия это не
мелочь, а сломанное разрешение. Нормативный текст обязан жить ОТДЕЛЬНО от пакета с кодом:
движок на него ссылается, а своей копии не держит. Иначе копия тихо разойдётся с текстом, и
мы будем выполнять одно, а обещать другое.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Официальный текст Apache License 2.0 с сайта Apache Software Foundation.
CANONICAL_LICENSE_SHA256 = "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"

CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def spec_dir() -> Path:
    """Дом нормативного текста. Переезд задаётся переменной окружения, не правкой теста."""
    override = os.environ.get("ARLI_SPEC_DIR")
    return Path(override) if override else ROOT.parent / "arli-spec"


def require_spec_house() -> Path:
    """Нормативный текст живёт отдельным домом. В чистом клоне его рядом нет.

    Это не повод молча пропустить проверку: причина называется прямо, чтобы отсутствие
    текста не выглядело как зелёный прогон.
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
    # знак не наследуется от лицензии: это должно быть сказано прямо
    assert "not granted by the" in policy
    assert "conformance suite v" in policy


def test_contributing_terms_are_inbound_equals_outbound():
    terms = _read("CONTRIBUTING.md")
    assert "Inbound equals outbound" in terms
    assert "perpetual, worldwide, non-exclusive, royalty-free" in terms
    assert "git commit -s" in terms


def test_the_normative_text_lives_outside_the_engine():
    """Текст обещания обязан жить отдельно: движок ссылается, а копии не держит."""
    require_spec_house()
    assert not (ROOT / "spec").exists(), "the engine must not keep a copy of the normative text"


def test_the_engine_matches_the_version_of_the_text_it_depends_on():
    import arli_engine

    require_spec_house()
    english = spec_file("SPEC.en.md")
    assert arli_engine.EXTENSION_VERSION == "1.1.0"
    assert f"# ARLI Discovery Extension v{arli_engine.EXTENSION_VERSION}" in english
    assert arli_engine.EXTENSION_URI in english


def test_changing_the_spec_house_is_a_setting_not_a_code_edit():
    """Переезд задаётся переменной окружения, чтобы разделение не ломалось правкой тестов."""
    assert os.environ.get("ARLI_SPEC_DIR") is None or Path(os.environ["ARLI_SPEC_DIR"]).is_dir()


def test_the_engine_does_not_publish_the_text():
    """В колесе текста быть не должно: его публикуют отдельно от кода."""
    language = _read("pyproject.toml")
    assert "arli-spec" not in language or "SPEC.en.md" not in language
    assert not (ROOT / "docs" / "spec.html").exists(), "the spec page belongs to the spec house"


def test_package_metadata_is_publishable():
    """Метаданные пакета уходят наружу вместе с кодом, поэтому без кириллицы."""
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
    # код вышел: адреса обязаны быть названы, как и то, что замечания к коду пока на русском
    assert "published under Apache-2.0" in readme
    assert "https://github.com/dmitrypehovski/arli-engine" in readme
    assert "comments in the code are" in readme and "in Russian" in readme
    # ограничения обязаны быть в описании, а не только в спецификации
    assert "does not close online probing" in readme
    assert "not a unique human" in readme
