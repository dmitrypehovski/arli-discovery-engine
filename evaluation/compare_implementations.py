"""Two implementations, one client, one measurement.

Why this file exists. The Python engine is the readable reference and the Rust implementation is
the second one. The same client talks to both, which is itself the point: if the surface were not
the same, this script would not work at all. What it measures is the cost of one full matching
exchange on each side, so the question "is Python too slow" gets an answer instead of an opinion.

Honest bounds: one machine, local ports, small feature counts, and the medians of a few runs.
The Rust numbers come from an optimised build and the Python ones from CPython with the
cryptography package; a production deployment would differ in both directions.

Run: python -m evaluation.compare_implementations --rust-binary ../arli-engine-rs/target/release/arli-discovery-rs
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import time
from pathlib import Path

from arli_engine.client import AgentClient
from arli_engine.serve import build_pair, port_of

SIZES = (8, 32, 64)
REPEATS = 3


def features(prefix: str, count: int) -> list[str]:
    return [f"{prefix}-topic:{index}" for index in range(count)]


def exchange(client_a: AgentClient, client_b: AgentClient, *, size: int, floor: int = 1) -> dict:
    """One full exchange: keys, blind, sign, finish, record."""
    started = time.perf_counter()
    session_id = f"compare-{size}-{time.time_ns()}"
    holder = client_b.arli_psi_keys({"session_id": session_id})
    commit = client_a.arli_psi_commit({
        "session_id": session_id, "peer_name": "peer",
        "n": holder["n"], "e": holder["e"],
    })
    signed = client_b.arli_psi_sign({"session_id": session_id, "blinded": commit["blinded"]})
    finish = client_a.arli_psi_finish({
        "session_id": session_id, "n": holder["n"], "signed": signed["signed"],
        "holder_tokens": signed["holder_tokens"], "floor_basis_points": floor,
    })
    client_b.arli_psi_record({
        "session_id": session_id, "matched": finish["matched_features"],
        "floor_basis_points": floor,
    })
    elapsed = (time.perf_counter() - started) * 1000
    return {"ms": elapsed, "matched": len(finish["matched_features"]), "blinded": len(commit["blinded"])}


def wait_for(port: int, timeout: float = 15.0) -> None:
    import httpx

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/.well-known/agent-card.json", timeout=1)
            return
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    raise SystemExit(f"the agent on port {port} did not come up")


def measure_python(size: int) -> list[dict]:
    states, servers = build_pair(0, 0)
    try:
        # Same feature count on both sides, so the two implementations do the same work.
        states[0].features = type(states[0].features)(features("alpha", size))
        states[1].features = type(states[1].features)(features("alpha", size))
        port_a, port_b = port_of(servers[0]), port_of(servers[1])
        with AgentClient(f"http://127.0.0.1:{port_a}") as client_a, \
                AgentClient(f"http://127.0.0.1:{port_b}") as client_b:
            return [exchange(client_a, client_b, size=size) for _ in range(REPEATS)]
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()


def measure_rust(binary: Path, size: int) -> list[dict]:
    ports = (8871, 8872)
    feature_list = ",".join(features("alpha", size))
    processes = [
        subprocess.Popen(
            [str(binary), "--port", str(port), "--name", f"rs-{index}", "--features", feature_list],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for index, port in enumerate(ports)
    ]
    try:
        for port in ports:
            wait_for(port)
        with AgentClient(f"http://127.0.0.1:{ports[0]}") as client_a, \
                AgentClient(f"http://127.0.0.1:{ports[1]}") as client_b:
            return [exchange(client_a, client_b, size=size) for _ in range(REPEATS)]
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()


def summarise(runs: list[dict]) -> dict:
    return {
        "median_ms": round(statistics.median(run["ms"] for run in runs), 1),
        "min_ms": round(min(run["ms"] for run in runs), 1),
        "matched": runs[0]["matched"],
        "blinded": runs[0]["blinded"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rust-binary",
        default="../arli-engine-rs/target/release/arli-discovery-rs",
    )
    args = parser.parse_args()
    binary = Path(args.rust_binary).resolve()

    table = []
    for size in SIZES:
        python_runs = measure_python(size)
        rust_runs = measure_rust(binary, size) if binary.is_file() else []
        entry = {"features_per_side": size, "python": summarise(python_runs)}
        if rust_runs:
            rust = summarise(rust_runs)
            entry["rust"] = rust
            entry["ratio_python_over_rust"] = round(rust["median_ms"] and python_runs[0]["ms"] / rust["median_ms"], 1) if rust["median_ms"] else None
            entry["rust_matched_the_same"] = rust["matched"] == entry["python"]["matched"]
        table.append(entry)
        print(json.dumps(entry, ensure_ascii=False))

    print()
    print("features | python median | rust median | python / rust")
    for entry in table:
        rust_ms = entry.get("rust", {}).get("median_ms")
        ratio = entry.get("ratio_python_over_rust")
        print(f"{entry['features_per_side']:>8} | {entry['python']['median_ms']:>13} | "
              f"{rust_ms if rust_ms is not None else 'n/a':>11} | {ratio if ratio else 'n/a'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
