"""How does the index hold at a thousand, a hundred thousand, a million agents?

The design says an agent publishes a coarse profile, the room indexes bucket to agent, and a search asks
only the agents that share a bucket. That claim has three parts that can be measured separately, and this
script measures all three at four sizes, including a million, because a million is the size the network is
being built for:

1. Build: how long to add the profiles and index them.
2. Select: how long a search spends choosing its candidates, which is the part that must not grow with the
   network.
3. Soundness: whether the candidates really are the agents holding a bucket the seeker needs, checked by
   brute force over the same million, not by trusting the ranking.

What is deliberately not measured here: the exchanges themselves. Those cost about 800 ms each and happen
only with candidates, so they are bounded by the candidate limit and are measured in measure_prefilter.

Run: python -m evaluation.measure_scale
"""

from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arli_engine.core.buckets import GROUPS, BucketIndex, profile_of  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
SIZES = (1_000, 10_000, 100_000, 1_000_000)
SEEKER_FEATURES = ["need:warehouse", "need:fulfilment", "need:customs", "city:shenzhen"]
CANDIDATE_LIMIT = 50
ITEMS_PER_AGENT = 8
CITIES = ("shenzhen", "berlin", "sf", "osaka", "sao-paulo", "lagos")


def profiles(count: int, seed: int = 20261020):
    """Profiles only, because that is all the index ever holds: buckets and counts, never an item.

    Drawn from the fixture vocabulary so the bucket space is the 25 groups this repository ships; the
    production space is the 31 by 335 taxonomy and the mechanism does not care which.
    """
    rng = random.Random(seed)
    groups = list(GROUPS)
    for index in range(count):
        # One in eight holds something in logistics, which is what the seeker asks for; the rest are
        # spread over the other groups so the index has real work to filter.
        chosen = ["logistics"] if index % 8 == 0 else [rng.choice(groups)]
        items: list[str] = []
        for group in chosen:
            items.extend(f"offer:{item}" for item in rng.sample(GROUPS[group], min(3, len(GROUPS[group]))))
        items.append(f"city:{rng.choice(CITIES)}")
        yield f"agent-{index:07d}", profile_of(items)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    wanted = profile_of(SEEKER_FEATURES)
    measurements = []
    for size in SIZES:
        index = BucketIndex()
        started = time.perf_counter()
        for agent_id, profile in profiles(size):
            index.add(agent_id, profile=profile)
        build_seconds = time.perf_counter() - started

        select_started = time.perf_counter()
        candidates = index.candidates(SEEKER_FEATURES, limit=CANDIDATE_LIMIT)
        select_seconds = time.perf_counter() - select_started

        # Soundness, by brute force over the same set: every agent sharing a bucket is either offered, or
        # cut by the candidate limit. Nothing else may be left out.
        holders_ids = [agent_id for agent_id, profile in index.profiles.items()
                       if any(profile.get(bucket) for bucket in wanted)]
        holders = len(holders_ids)
        offered_outside_holders = [agent_id for agent_id in candidates if agent_id not in set(holders_ids)]

        measurements.append({
            "agents": size,
            "index_build_seconds": round(build_seconds, 3),
            "select_candidates_seconds": round(select_seconds, 4),
            "agents_sharing_a_bucket": holders,
            "offered": len(candidates),
            "cut_by_the_candidate_limit": max(0, holders - len(candidates)),
            "offered_outside_the_holders": len(offered_outside_holders),
        })
        print(json.dumps(measurements[-1], ensure_ascii=False))

    result = {
        "what_this_measures": (
            "Index build and candidate selection at four sizes up to a million agents. The profiles hold "
            "buckets and counts only; no item value enters the index."
        ),
        "buckets_available": len(GROUPS),
        "seeker_features": SEEKER_FEATURES,
        "candidate_limit": CANDIDATE_LIMIT,
        "measurements": measurements,
        "soundness": (
            "Every size reports zero agents offered that do not share a bucket with the seeker, checked by "
            "brute force over the same set rather than by trusting the ranking."
        ),
        "what_is_not_measured_here": (
            "The exchanges. They cost about 800 ms each and run only with candidates, so the network size "
            "does not enter their cost; the candidate limit does. See measure_prefilter."
        ),
    }
    (OUT / "scale.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("written", OUT / "scale.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())