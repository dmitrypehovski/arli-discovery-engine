"""Build the plugin manifest from the engine surface.

Run: python -m tools.build_plugin_manifest [path]
Without a path it writes next to the adapter. The manifest is deterministic, so the
written file is checked against the built one in tests: a mismatch means someone edited
the file by hand.
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
    print(f"plugin manifest written: {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
