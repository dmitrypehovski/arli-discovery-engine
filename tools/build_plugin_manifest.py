"""Сборка описи плагина из поверхности движка.

Запуск: python -m tools.build_plugin_manifest [путь]
Без пути пишет рядом с адаптером. Опись детерминирована, поэтому записанный файл
сверяется с собранным в тестах: расхождение означает, что кто-то правил файл руками.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arli_engine.adapters.plugin.manifest import manifest_path, write_manifest  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    target = Path(argv[0]) if argv else manifest_path()
    written = write_manifest(target)
    print(f"опись плагина записана: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
