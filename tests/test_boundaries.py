"""Граница слоёв: ядро не знает про транспорт.

Правило из раздела 10 плана: из ``arli_engine/core`` не должно быть ни одного
импорта сети, потоков и HTTP. Проверяется тестом, а не обещанием.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CORE = Path(__file__).resolve().parents[1] / "arli_engine" / "core"

# запрещённые корневые модули: сеть, потоки, HTTP
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
    """Тест не должен проходить вхолостую на пустом каталоге."""
    assert CORE_FILES, f"в {CORE} нет файлов для проверки"


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_core_does_not_import_transport(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            root = _root_of(node)
            if root in FORBIDDEN:
                offenders.append(f"строка {node.lineno}: {root}")
    assert not offenders, f"{path.name} импортирует транспорт: {offenders}"


@pytest.mark.parametrize("path", CORE_FILES, ids=lambda p: p.name)
def test_core_imports_are_absolute_engine_paths(path: Path) -> None:
    """Ядро импортирует только себя, через абсолютный путь пакета."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            pytest.fail(f"{path.name} строка {node.lineno}: относительный импорт вместо абсолютного")
