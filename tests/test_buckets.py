"""The bucket profile: a search talks to candidates, not to everyone.

The index is what makes a room of thousands searchable at all, so two properties are worth holding: it
offers only agents that share a bucket with the seeker, and it never holds an item value.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arli_engine.core.buckets import GROUPS, BucketIndex, bucket_of, profile_of  # noqa: E402


def test_a_profile_counts_buckets_and_never_names_an_item() -> None:
    profile = profile_of(["need:warehouse", "need:fulfilment", "city:shenzhen", "offer:brand-design"])
    assert profile == {"logistics": 2, "design": 1}, profile
    assert not any("warehouse" in key or "shenzhen" in key for key in profile), (
        "a published profile carries buckets and counts, never an item"
    )


def test_an_item_the_fixture_does_not_know_is_not_searchable_by_bucket() -> None:
    assert bucket_of("need:warehouse") == "logistics"
    assert bucket_of("need:warehouse") == bucket_of("offer:warehouse"), "one item, two spellings"
    assert bucket_of("need:something-nobody-listed") is None, (
        "an unknown item has no bucket, and the text must say such an agent is reachable by address"
    )


def test_only_agents_sharing_a_bucket_are_offered() -> None:
    index = BucketIndex()
    index.add("logistics-only", features=["offer:warehouse", "offer:freight"])
    index.add("design-only", features=["offer:brand-design", "offer:ux"])
    index.add("both", features=["offer:warehouse", "offer:brand-design"])
    index.add("nothing-shared", features=["offer:solar", "offer:battery"])

    candidates = index.candidates(["need:warehouse", "need:brand-design"], limit=10)
    assert set(candidates) == {"both", "logistics-only", "design-only"}, candidates
    assert "nothing-shared" not in candidates, "an agent sharing no bucket is not worth an exchange"
    assert candidates[0] == "both", "the agent sharing more buckets comes first"


def test_the_limit_is_a_budget_and_the_index_says_so_by_its_size() -> None:
    index = BucketIndex()
    for number in range(120):
        index.add(f"agent-{number:03d}", features=["offer:warehouse"])
    assert index.size() == 120
    assert len(index.candidates(["need:warehouse"], limit=50)) == 50, (
        "the limit bounds a search, which is why a result must say how many candidates were tried"
    )
    assert len(index.candidates(["need:warehouse"], limit=200)) == 120


def test_the_fixture_covers_the_space_it_claims() -> None:
    assert len(GROUPS) == 25
    assert all(len(items) == 20 for items in GROUPS.values()), "twenty items per bucket in the fixture"
