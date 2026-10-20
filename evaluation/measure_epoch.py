"""What does one key per epoch buy, and what stays per-exchange?

Two costs were paid on every single exchange: generating an RSA key, and recomputing signatures over the
holder's own list. The epoch removes both from the steady state and keeps the one part that cannot be
removed, signing the seeker's blinded values. This script measures a cold exchange (epoch just made) against
a warm one (epoch reused), and names how much of the cold cost was the key.

Run: python -m evaluation.measure_epoch
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arli_engine.core import psi  # noqa: E402
from arli_engine.serve import build_pair, port_of  # noqa: E402
from arli_engine.client import AgentClient  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
FEATURES = 32
ROUNDS = 5


def one_exchange(client: AgentClient, tag: str) -> float:
    started = time.perf_counter()
    keys = client.call("arli/psi/keys", {"session_id": tag})
    blinded = client.call("arli/psi/commit", {
        "session_id": tag, "peer_name": "seeker", "n": keys["n"], "e": keys["e"],
    })
    client.call("arli/psi/sign", {"session_id": tag, "blinded": blinded["blinded"][:FEATURES]})
    return time.perf_counter() - started


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)

    keygen_started = time.perf_counter()
    psi.generate_keys()
    keygen_ms = (time.perf_counter() - keygen_started) * 1000

    states, servers = build_pair(0, 0, names=("holder", "seeker"))
    holder = states[1]  # the agent answering psi/keys and psi/sign
    holder.features = [f"offer:item-{number:02d}" for number in range(FEATURES)]
    url = f"http://127.0.0.1:{port_of(servers[1])}"
    cold: list[float] = []
    warm: list[float] = []
    tokens_recomputed: list[int] = []
    try:
        with AgentClient(url) as client:
            for round_number in range(ROUNDS):
                # Cold: the epoch is thrown away, so the key and the token table are made again, which is
                # exactly what every exchange used to pay. The token cache is cleared by the same act.
                holder.epoch = None  # what every exchange used to do: a key and a token table, again
                cold.append(one_exchange(client, f"cold-{round_number}"))
                tokens_recomputed.append(len(holder.epoch._tokens) if holder.epoch else 0)
            for round_number in range(ROUNDS):
                warm.append(one_exchange(client, f"warm-{round_number}"))
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()

    cold_median = statistics.median(cold)
    warm_median = statistics.median(warm)
    result = {
        "what_this_measures": (
            "One key per epoch instead of one key per exchange, and the holder's own tokens computed once "
            "per epoch instead of once per seeker."
        ),
        "features_each_side": FEATURES,
        "key_generation_ms": round(keygen_ms, 1),
        "cold_exchange_median_ms": round(cold_median * 1000, 1),
        "warm_exchange_median_ms": round(warm_median * 1000, 1),
        "cold_exchanges": [round(value * 1000, 1) for value in cold],
        "warm_exchanges": [round(value * 1000, 1) for value in warm],
        "key_share_of_a_cold_exchange": round(keygen_ms / (cold_median * 1000), 3),
        "what_stays_per_exchange": (
            "Signing the seeker's blinded values, which only the holder can do. The epoch removes key "
            "generation and token recomputation from the steady state; it does not remove that."
        ),
        "tokens_recomputed_per_cold_exchange": tokens_recomputed,
        "searches_per_second_warm": round(1 / warm_median, 2) if warm_median else None,
    }
    (OUT / "epoch.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "written": str(OUT / "epoch.json"),
        "keygen_ms": result["key_generation_ms"],
        "cold_ms": result["cold_exchange_median_ms"],
        "warm_ms": result["warm_exchange_median_ms"],
        "key_share": result["key_share_of_a_cold_exchange"],
        "warm_per_second": result["searches_per_second_warm"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())