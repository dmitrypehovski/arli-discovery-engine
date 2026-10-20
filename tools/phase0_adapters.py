#!/usr/bin/env python3
"""Phase 0, task 0.4: move the A2A adapter and the example into the engine.

We move without changing behavior: we only fix import paths and add a single new
block, raising the repository root in the example, otherwise
`python examples/two_agents/main.py` will not find the `arli_engine` package.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

ORG = Path("/home/paperclip/arli-org")
STAND = ORG / "proto-a2a-discovery"
ENGINE = ORG / "arli-engine"

# A single import path in the engine, so there are not two sources of truth.
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

# Files moving into the engine: source, destination.
MOVES = [
    (STAND / "arli_a2a" / "server.py", ENGINE / "arli_engine" / "adapters" / "a2a" / "server.py"),
    (STAND / "run_demo.py", ENGINE / "examples" / "two_agents" / "main.py"),
    (STAND / "tests" / "test_server.py", ENGINE / "tests" / "test_a2a_server.py"),
]

BOOTSTRAP = (
    "import sys as _sys\n"
    "from pathlib import Path as _Path\n"
    "\n"
    "# The example runs from the repository root, so we add the root to the path ourselves.\n"
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
            print(f"no source: {src}")
            return 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = rewrite(src.read_text(encoding="utf-8"))
        if dst.name == "main.py":
            text = text.replace(
                "Run:\n    .venv/bin/python run_demo.py",
                "Run (from the repository root):\n    .venv/bin/python examples/two_agents/main.py",
            )
            # place the root bootstrap before the first engine import
            text = text.replace(
                "from arli_engine import (",
                BOOTSTRAP + "\nfrom arli_engine import (",
                1,
            )
        dst.write_text(text, encoding="utf-8")
        src.unlink()
        print(f"moved: {src.name} → {dst.relative_to(ORG)}")

    # package export: the same as before, but along the new paths
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
    print("updated the arli_engine package export")

    # the stand directory no longer holds code: leave a pointer so there are not two truths
    shutil.rmtree(STAND / "arli_a2a", ignore_errors=True)
    (STAND / "README.md").write_text(
        "# The stand moved\n\nThe live stand moved to `arli-engine/`: the core code in `arli_engine/core`,\n"
        "the A2A adapter in `arli_engine/adapters/a2a`, the two-agent run in\n"
        "`examples/two_agents/main.py`. Only the run results (`out/`) remain here,\n"
        "along with the virtual environment used to run the engine tests.\n",
        encoding="utf-8",
    )
    print("the stand is left only as a directory with results and an environment")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
