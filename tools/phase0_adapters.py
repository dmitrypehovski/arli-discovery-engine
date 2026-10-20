#!/usr/bin/env python3
"""Фаза 0, задача 0.4: перенос адаптера A2A и примера в движок.

Переносим без изменения поведения: правим только пути импортов и добавляем
единственную новую строку, подъём корня репозитория в примере, иначе
`python examples/two_agents/main.py` не найдёт пакет `arli_engine`.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

ORG = Path("/home/paperclip/arli-org")
STAND = ORG / "proto-a2a-discovery"
ENGINE = ORG / "arli-engine"

# Один путь импорта в движке, чтобы не было двух источников правды.
IMPORT_MAP = [
    ("from arli_a2a.cards import", "from arli_engine.adapters.a2a.cards import"),
    ("from arli_a2a.effects import", "from arli_engine.core.capability import"),
    ("from arli_a2a.evidence import", "from arli_engine.core.evidence import"),
    ("from arli_a2a.matching import", "from arli_engine.core.match import"),
    ("from arli_a2a.negotiation import", "from arli_engine.core.negotiate import"),
    ("from arli_a2a.signing import", "from arli_engine.core.signing import"),
    ("from arli_a2a.server import", "from arli_engine.adapters.a2a.server import"),
    ("from arli_a2a import", "from arli_engine import"),
    ("from .effects import", "from arli_engine.core.capability import"),
    ("from .evidence import", "from arli_engine.core.evidence import"),
    ("from .matching import", "from arli_engine.core.match import"),
    ("from .negotiation import", "from arli_engine.core.negotiate import"),
]

# Файлы, переезжающие в движок: источник, цель.
MOVES = [
    (STAND / "arli_a2a" / "server.py", ENGINE / "arli_engine" / "adapters" / "a2a" / "server.py"),
    (STAND / "run_demo.py", ENGINE / "examples" / "two_agents" / "main.py"),
    (STAND / "tests" / "test_server.py", ENGINE / "tests" / "test_a2a_server.py"),
]

BOOTSTRAP = (
    "import sys as _sys\n"
    "from pathlib import Path as _Path\n"
    "\n"
    "# Пример запускается из корня репозитория, поэтому корень поднимаем в путь сами.\n"
    "_ROOT = _Path(__file__).resolve().parents[2]\n"
    "if str(_ROOT) not in _sys.path:\n"
    "    _sys.path.insert(0, str(_ROOT))\n"
)


def rewrite(text: str) -> str:
    for old, new in IMPORT_MAP:
        text = text.replace(old, new)
    return text


def main() -> int:
    for src, dst in MOVES:
        if not src.exists():
            print(f"нет источника: {src}")
            return 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = rewrite(src.read_text(encoding="utf-8"))
        if dst.name == "main.py":
            text = text.replace(
                "Запуск:\n    .venv/bin/python run_demo.py",
                "Запуск (из корня репозитория):\n    .venv/bin/python examples/two_agents/main.py",
            )
            # подъём корня ставим до первого импорта движка
            text = text.replace(
                "from arli_engine import (",
                BOOTSTRAP + "\nfrom arli_engine import (",
                1,
            )
        dst.write_text(text, encoding="utf-8")
        src.unlink()
        print(f"перенесён: {src.name} → {dst.relative_to(ORG)}")

    # экспорт пакета: то же, что было, но по новым путям
    init = ENGINE / "arli_engine" / "__init__.py"
    exports = (
        "\nfrom arli_engine.adapters.a2a.cards import EXTENSION_URI, build_card, capability_document\n"
        "from arli_engine.core.canonical import canonical_json, canonicalize\n"
        "from arli_engine.core.capability import EFFECT_KEYS, Operation, capability_manifest\n"
        "from arli_engine.core.evidence import Completeness, Evidence\n"
        "from arli_engine.core.match import FeatureSet, PairSession, local_match\n"
        "from arli_engine.core.negotiate import MAX_TURNS, Negotiation, NegotiationError, Opportunity\n"
        "from arli_engine.core.signing import KeyPair, sign_card, verify_card\n"
        "\n__all__ = [\n"
        '    "EXTENSION_URI",\n    "EFFECT_KEYS",\n    "MAX_TURNS",\n    "Completeness",\n'
        '    "Evidence",\n    "FeatureSet",\n    "KeyPair",\n    "Negotiation",\n'
        '    "NegotiationError",\n    "Operation",\n    "Opportunity",\n    "PairSession",\n'
        '    "build_card",\n    "canonical_json",\n    "canonicalize",\n'
        '    "capability_document",\n    "capability_manifest",\n    "local_match",\n'
        '    "sign_card",\n    "verify_card",\n]\n'
    )
    body = init.read_text(encoding="utf-8").rstrip() + "\n"
    init.write_text(body + exports, encoding="utf-8")
    print("обновлён экспорт пакета arli_engine")

    # каталог стенда больше не содержит кода: оставляем указатель, чтобы не было двух правд
    shutil.rmtree(STAND / "arli_a2a", ignore_errors=True)
    (STAND / "README.md").write_text(
        "# Стенд перенесён\n\nЖивой стенд переехал в `arli-engine/`: код ядра в `arli_engine/core`,\n"
        "адаптер A2A в `arli_engine/adapters/a2a`, прогон двух агентов в\n"
        "`examples/two_agents/main.py`. Здесь остались только результаты прогона (`out/`)\n"
        "и виртуальное окружение, которым гоняются тесты движка.\n",
        encoding="utf-8",
    )
    print("стенд оставлен только как каталог с результатами и окружением")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
