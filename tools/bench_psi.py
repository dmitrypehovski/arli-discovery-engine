"""Замер двух схем пересечения: задача 1.5 плана, шаг 1.

Считаем время работы каждой стороны и объём обмена для двух схем:
- пересечение по хешам (то, что в движке сейчас): n отпечатков по 32 байта;
- закрытое пересечение на слепом подписывании RSA: 3n значений по 257 байт.

Запуск: `.venv/bin/python tools/bench_psi.py`
Ничего не меняет: только считает и печатает.
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
MODULUS_BYTES = 257  # 2048-битный модуль плюс байт на знак
DIGEST_BYTES = 32


def features(count: int, prefix: str) -> list[str]:
    return [f"{prefix}:{i}" for i in range(count)]


def bench_hashes(n: int) -> dict:
    """Пересечение по хешам: сколько считает каждая сторона и сколько уходит в провод."""
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
    """Закрытое пересечение: те же поля, но с экспоненцированием на каждой стороне."""
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
    print("признаков |           схема | сторона A, мс | сторона B, мс | провод, байт")
    print("----------|-----------------|---------------|---------------|-------------")
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
    print(f"\nзаписан замер: {out / 'bench_intersection.json'}")
    print("оговорка: экспоненцирование делает C-реализация больших чисел Python,")
    print("поэтому это оценка для схемы на больших числах, не для кривой через OpenSSL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
