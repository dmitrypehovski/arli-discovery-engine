"""Does a bucket profile turn a search from one exchange per agent into a few exchanges?

The claim is that a room of thousands, and a network of millions, stays searchable because an agent
publishes a coarse profile and the search talks only to agents sharing a bucket. A claim like that is worth
nothing until it is measured, so this script builds a thousand agents, indexes them, and answers three
questions with numbers:

1. How many exchanges does a search attempt with the prefilter, and how long does it take.
2. How many it would attempt without one, over the same room, and how long that would take. The per-exchange
   cost is measured on a sample and multiplied out, because an exchange is independent of the others and
   the full thousand would take most of an hour; the extrapolation is labelled as such in the output.
3. Are the matches the same? For every agent that both paths reach, the matched items must be identical, or
   the prefilter is not a shortcut but a different answer.

Run: python -m evaluation.measure_prefilter
"""

from __future__ import annotations

import json
import os
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arli_engine.core.buckets import GROUPS, BucketIndex, bucket_of, profile_of  # noqa: E402
from demo.exchange import run_exchange  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
AGENTS = 1000
SEEKER_FEATURES = ["need:warehouse", "need:fulfilment", "need:customs", "city:shenzhen"]
CANDIDATE_LIMIT = 50
SAMPLE_FOR_FULL = 25  # exchanges actually run to obtain a per-exchange cost for the extrapolation
IDENTITY_CHECK = 10  # agents reached by both paths whose matches are compared


def build_agents(count: int, seed: int = 20261020) -> list[tuple[str, list[str]]]:
    """A thousand agents with plausible short lists drawn from the fixture vocabulary."""
    rng = random.Random(seed)
    groups = list(GROUPS)
    agents = []
    for index in range(count):
        # Roughly half of them hold something in logistics, so the seeker's list has real work to do.
        chosen = ["logistics"] if index % 2 == 0 else rng.sample(groups, rng.choice([1, 2, 3]))
        features = []
        for group in chosen:
            items = GROUPS[group]
            features.extend(f"offer:{item}" for item in rng.sample(items, min(3, len(items))))
        features.append(f"city:{rng.choice(['shenzhen', 'berlin', 'sf', 'osaka', 'berlin'])}")
        agents.append((f"agent-{index:04d}", features))
    return agents


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()

    agents = build_agents(AGENTS)
    index_started = time.perf_counter()
    index = BucketIndex()
    for agent_id, features in agents:
        index.add(agent_id, features=features)
    index_seconds = time.perf_counter() - index_started

    candidates = index.candidates(SEEKER_FEATURES, limit=CANDIDATE_LIMIT)

    features_of = {agent_id: features for agent_id, features in agents}

    def one_exchange(agent_id: str) -> dict:
        return run_exchange(SEEKER_FEATURES, features_of[agent_id], 3000, seeker_name="seeker",
                            holder_name=agent_id, seeker_kind="demand", holder_kind="supply")

    # With the prefilter: real exchanges, only with candidates.
    with_started = time.perf_counter()
    prefiltered: dict[str, dict] = {}
    for agent_id in candidates:
        prefiltered[agent_id] = one_exchange(agent_id)
    with_seconds = time.perf_counter() - with_started

    # Without it: every exchange below was run, the rest is the same work repeated.
    sample_started = time.perf_counter()
    sampled: dict[str, dict] = {}
    for agent_id, _features in agents[:SAMPLE_FOR_FULL]:
        sampled[agent_id] = one_exchange(agent_id)
    sample_seconds = time.perf_counter() - sample_started
    per_exchange = sample_seconds / SAMPLE_FOR_FULL
    full_seconds = per_exchange * len(agents)

    matched_prefiltered = [agent_id for agent_id, result in prefiltered.items() if result["matched_features"]]
    matched_sampled = [agent_id for agent_id, result in sampled.items() if result["matched_features"]]

    # The identity check: for agents both paths reached, the matches must be the same, item for item.
    both = [agent_id for agent_id in candidates if agent_id in sampled][:IDENTITY_CHECK]
    identical = all(
        prefiltered[agent_id]["matched_features"] == sampled[agent_id]["matched_features"]
        for agent_id in both
    )
    differences = [
        {"agent": agent_id,
         "prefiltered": prefiltered[agent_id]["matched_features"],
         "full": sampled[agent_id]["matched_features"]}
        for agent_id in both
        if prefiltered[agent_id]["matched_features"] != sampled[agent_id]["matched_features"]
    ]

    # A search that missed a real match would be a shortcut with a hole in it: how many agents holding the
    # seeker's items does the prefilter fail to offer?
    # By token, not by string: need:warehouse and offer:warehouse are one item, and comparing raw strings
    # here would measure the wrong thing entirely.
    from arli_engine.core.vocabulary import canonical_token

    seeker_tokens = {canonical_token(feature) for feature in SEEKER_FEATURES}
    holders = [agent_id for agent_id, features in agents
               if any(canonical_token(feature) in seeker_tokens for feature in features)]
    missed = [agent_id for agent_id in holders if agent_id not in candidates]

    result = {
        "what_this_measures": (
            "A bucket profile turns a search into exchanges with candidates instead of one exchange per "
            "agent. Numbers here come from one machine and local ports."
        ),
        "room": {
            "agents": len(agents),
            "buckets_available": len(GROUPS),
            "index_build_seconds": round(index_seconds, 3),
        },
        "seeker": {"features": SEEKER_FEATURES, "candidate_limit": CANDIDATE_LIMIT},
        "candidates": len(candidates),
        "exchanges": {
            "with_prefilter": len(candidates),
            "with_prefilter_seconds": round(with_seconds, 2),
            "without_prefilter": len(agents),
            "without_prefilter_seconds_measured_sample": round(sample_seconds, 2),
            "without_prefilter_seconds_extrapolated": round(full_seconds, 1),
            "per_exchange_ms": round(per_exchange * 1000, 1),
            "extrapolation": f"per-exchange cost measured on {SAMPLE_FOR_FULL} agents and multiplied by "
                             f"{len(agents)}; exchanges are independent of each other",
        },
        "matches": {
            "with_prefilter": matched_prefiltered,
            "sampled_full_scan": matched_sampled,
        },
        "identical_results_for_shared_agents": identical,
        "identical_checked_on": both,
        "differences": differences,
        "holders_of_the_seeker_items": len(holders),
        "holders_not_offered": missed,
        "missed_because_of_the_candidate_limit": max(0, len(holders) - CANDIDATE_LIMIT),
        "note_on_misses": (
            "Two separate reasons an agent holding a shared item is not offered. One: the candidate limit, "
            "which makes a search budgeted rather than complete, and which a product must state plainly. "
            "Two: an item whose bucket the taxonomy does not know, which is reachable by address but not by "
            "bucket search. Neither is hidden in the result: the search says how many candidates it tried."
        ),
        "hub_agents": {
            "total": len(agents),
            "agents_holding_a_seeker_item": len(holders),
        },
        "elapsed_seconds": round(time.perf_counter() - started, 1),
    }
    (OUT / "prefilter.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "written": str(OUT / "prefilter.json"),
        "agents": len(agents),
        "buckets": len(GROUPS),
        "index_seconds": result["room"]["index_build_seconds"],
        "candidates": len(candidates),
        "exchanges_with_prefilter": len(candidates),
        "seconds_with_prefilter": result["exchanges"]["with_prefilter_seconds"],
        "exchanges_without_prefilter": len(agents),
        "seconds_without_prefilter_extrapolated": result["exchanges"]["without_prefilter_seconds_extrapolated"],
        "identical": identical,
        "holders_not_offered": len(missed),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())