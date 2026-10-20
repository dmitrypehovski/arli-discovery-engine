"""Task 0.2: move the core, replacing relative imports with absolute ones."""
import re
import shutil
from pathlib import Path

STAND = Path("/home/paperclip/arli-org/proto-a2a-discovery/arli_a2a")
STD_TESTS = Path("/home/paperclip/arli-org/proto-a2a-discovery/tests")
ENG = Path("/home/paperclip/arli-org/arli-engine")

# source module -> target path
MOVES = {
    "canonical.py": "arli_engine/core/canonical.py",
    "effects.py": "arli_engine/core/capability.py",
    "evidence.py": "arli_engine/core/evidence.py",
    "matching.py": "arli_engine/core/match.py",
    "negotiation.py": "arli_engine/core/negotiate.py",
    "signing.py": "arli_engine/core/signing.py",
    "cards.py": "arli_engine/adapters/a2a/cards.py",
}

TEST_MOVES = {
    "test_core.py": "tests/test_core.py",
    "test_rules.py": "tests/test_rules.py",
}

# stand module -> absolute path in the engine
ABS = {
    "canonical": "arli_engine.core.canonical",
    "effects": "arli_engine.core.capability",
    "evidence": "arli_engine.core.evidence",
    "matching": "arli_engine.core.match",
    "negotiation": "arli_engine.core.negotiate",
    "signing": "arli_engine.core.signing",
    "cards": "arli_engine.adapters.a2a.cards",
    "server": "arli_engine.adapters.a2a.server",
}


def rewrite(text: str) -> str:
    # imports can sit inside functions, so we account for indentation
    ws = r"^([ \t]*)"
    for mod, target in ABS.items():
        text = re.sub(rf"{ws}from \.{mod} import ", rf"\1from {target} import ", text, flags=re.M)
        text = re.sub(rf"{ws}import \.{mod}\b", rf"\1import {target}", text, flags=re.M)
    # absolute imports of the stand
    for mod, target in ABS.items():
        text = re.sub(rf"{ws}from arli_a2a\.{mod} import ", rf"\1from {target} import ", text, flags=re.M)
    text = re.sub(rf"{ws}from arli_a2a import ", r"\1from arli_engine import ", text, flags=re.M)
    text = re.sub(rf"{ws}import arli_a2a\b", r"\1import arli_engine", text, flags=re.M)
    return text


def ensure_pkg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not (path.parent / "__init__.py").exists():
        (path.parent / "__init__.py").write_text("", encoding="utf-8")


def main() -> None:
    for src_name, dst_rel in MOVES.items():
        src = STAND / src_name
        dst = ENG / dst_rel
        ensure_pkg(dst)
        dst.write_text(rewrite(src.read_text(encoding="utf-8")), encoding="utf-8")
        print(f"moved  {dst_rel}")

    for src_name, dst_rel in TEST_MOVES.items():
        src = STD_TESTS / src_name
        dst = ENG / dst_rel
        dst.write_text(rewrite(src.read_text(encoding="utf-8")), encoding="utf-8")
        print(f"moved  {dst_rel}")

    # the adapters package must be importable
    for rel in ("arli_engine/adapters/__init__.py", "arli_engine/adapters/a2a/__init__.py"):
        p = ENG / rel
        if not p.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("", encoding="utf-8")
            print(f"created    {rel}")


if __name__ == "__main__":
    main()
