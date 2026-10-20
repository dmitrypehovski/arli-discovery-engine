"""Benchmark of two intersection schemes: task 1.5 of the plan, step 1.

We measure the running time of each side and the exchange size for two schemes:
- hash intersection (what the engine uses now): n commitments of 32 bytes each;
- closed intersection on blind RSA signing: 3n values of 257 bytes each.

Run: `.venv/bin/python tools/bench_psi.py`
It changes nothing: it only computes and prints.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from arli_engine.core.crypto import commitment, pair_label  # noqa: E402
from arli_engine.core.psi import blind, generate_keys, own_signed_tokens, sign, unblind  # noqa: E402

SIZES = (10, 100, 1000)
REPEATS = 5
MODULUS_BYTES = 257  # a 2048-bit modulus plus a byte for the sign
DIGEST_BYTES = 32


def features(count: int, prefix: str) -> list[str]:
    return [f"{prefix}:{i}" for i in range(count)]


def bench_hashes(n: int) -> dict:
    """Hash intersection: how much each side computes and how much goes onto the wire."""
    key = "33" * 32
    session = "s-bench"
    pair = pair_label("agent-a", "agent-b")
    own = features(n, "own")
    theirs = features(n, "theirs")

    times_a, times_b = [], []
    for _ in range(REPEATS):
        t0 = time.perf_counter()
        commitments_a = [commitment(key, session_id=session, pair=pair, feature=f) for f in own]
        times_a.append(time.perf_counter() - t0)
        t0 = time.perf_counter()
        commitments_b = [commitment(key, session_id=session, pair=pair, feature=f) for f in theirs]
        times_b.append(time.perf_counter() - t0)
    overlap = len(set(commitments_a) & set(commitments_b))
    return {
        "scheme": "hash intersection",
        "side_a_ms": round(statistics.median(times_a) * 1000, 3),
        "side_b_ms": round(statistics.median(times_b) * 1000, 3),
        "bytes_on_wire": 2 * n * DIGEST_BYTES,
        "overlap_found": overlap,
    }


def bench_psi(n: int) -> dict:
    """Closed intersection: the same fields, but with exponentiation on each side."""
    keys = generate_keys()
    unrelated = features(n, "aside")
    holder_view = unrelated[: n // 2] + features(n - n // 2, "shared")

    times_a, times_b = [], []
    for _ in range(REPEATS):
        t0 = time.perf_counter()
        blinded = [blind(f, keys.n, keys.e) for f in unrelated]
        times_a.append(time.perf_counter() - t0)

        t0 = time.perf_counter()
        signed_back = [sign(b, keys.d, keys.n) for b, _ in blinded]
        holder_tokens = own_signed_tokens(holder_view, keys)
        times_b.append(time.perf_counter() - t0)

        unblinded = {unblind(s, r, keys.n) for s, (_, r) in zip(signed_back, blinded)}
        overlap = len(unblinded & set(holder_tokens))

    return {
        "scheme": "closed intersection (blind RSA)",
        "side_a_ms": round(statistics.median(times_a) * 1000, 3),
        "side_b_ms": round(statistics.median(times_b) * 1000, 3),
        "bytes_on_wire": 3 * n * MODULUS_BYTES,
        "overlap_found": overlap,
    }


def main() -> int:
    rows = []
    print("features |            scheme | side A, ms | side B, ms | wire, bytes")
    print("---------|-------------------|------------|------------|-------------")
    for n in SIZES:
        for bench in (bench_hashes, bench_psi):
            row = bench(n)
            row["features"] = n
            rows.append(row)
            print(
                f"{n:9d} | {row['scheme']:>15} | {row['side_a_ms']:>13} | "
                f"{row['side_b_ms']:>13} | {row['bytes_on_wire']:>11}"
            )
    out = Path(__file__).resolve().parents[1] / "out"
    out.mkdir(exist_ok=True)
    (out / "bench_intersection.json").write_text(
        json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"\nbenchmark written: {out / 'bench_intersection.json'}")
    print("caveat: the exponentiation is done by Python's C implementation of big numbers,")
    print("so this is an estimate for a big-number scheme, not for a curve through OpenSSL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
