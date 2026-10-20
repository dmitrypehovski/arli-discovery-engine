"""Tests for ARLI Match, the product built on the engine.

Why these exist. The page promises three things a reader cannot check by looking: every row came
from a real exchange, pairs that do not face are refused rather than scored, and no plain feature
reaches the wire. Each promise gets a test that fails when it stops being true.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.server import DEFAULT_MINE, ROSTER, public_view, scan  # noqa: E402


def test_the_room_publishes_identity_but_never_features() -> None:
    view = public_view(ROSTER[0])
    assert set(view) == {"id", "name", "kind", "city", "headline", "claim_level",
                         "will_not_publish", "feature_count"}
    for agent in ROSTER:
        assert "features" not in public_view(agent), agent["id"]
        # The published view must not leak a single feature string by accident.
        blob = " ".join(str(value) for value in public_view(agent).values())
        for feature in agent["features"]:
            assert feature not in blob, f"{agent['id']} publishes {feature}"


def test_a_scan_ranks_by_overlap_and_keeps_the_wires_clean() -> None:
    result = scan(DEFAULT_MINE, 3000, "demand")
    assert result["roster_kind"] == "synthetic"
    assert result["agents_checked"] == len(ROSTER)
    assert result["wires_clean"] is True, "some feature reached a wire"

    opened = [row for row in result["rows"] if row["opened"]]
    refused = [row for row in result["rows"] if not row["opened"]]
    assert opened and refused, "the scenario should contain both outcomes"

    basis = [row["overlap_basis_points"] for row in opened]
    assert basis == sorted(basis, reverse=True), "the board must be ranked by overlap"
    for row in refused:
        assert row["rejection"] == "types_not_comparable", row["name"]
        assert row["overlap_basis_points"] is None, "a refused pair is not compared at all"

    for row in opened:
        if row["passed_floor"]:
            assert row["matched_features"], "above the floor something must come back"


def test_a_buyer_does_not_pair_with_another_buyer() -> None:
    as_buyer = scan(DEFAULT_MINE, 3000, "demand")
    buyers = [row for row in as_buyer["rows"] if row["kind"] == "demand"]
    assert buyers, "the room needs at least one other buyer for this test to mean anything"
    assert all(not row["opened"] for row in buyers)

    # The same room seen as a seller pairs with those buyers instead.
    as_seller = scan(DEFAULT_MINE, 3000, "supply")
    sellers = [row for row in as_seller["rows"] if row["kind"] == "supply"]
    assert all(not row["opened"] for row in sellers)
    assert any(row["opened"] for row in as_seller["rows"] if row["kind"] == "demand")
