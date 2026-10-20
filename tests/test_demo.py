"""Tests for the live demonstration.

Why these exist. The demonstration makes a claim on a public page: no feature leaves either side.
A claim on a page is worth nothing unless something fails when it stops being true, so the exchange
is run here for real and the wire is searched for the feature strings.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from demo.server import FLOOR_BASIS_POINTS, SCENARIO, run_exchange  # noqa: E402


def _run(floor: int = FLOOR_BASIS_POINTS) -> dict:
    return run_exchange(
        list(SCENARIO["seeker"]["features"]),
        list(SCENARIO["holder"]["features"]),
        floor,
    )


def test_the_exchange_runs_end_to_end() -> None:
    result = _run()
    assert result["steps"], "the transcript cannot be empty"
    assert result["agreed"] is True, "the agents are supposed to agree in the scenario"
    assert len(result["steps"]) == 9, "nine calls are what the page says there are"
    assert all(card["verified"] for card in result["cards"]), "both cards must verify"


def test_no_feature_reaches_the_wire() -> None:
    result = _run()
    assert result["leak_check"]["clean"] is True, result["leaked_features"]
    assert result["leak_check"]["checked_steps"] == len(result["steps"])


def test_the_floor_decides_what_comes_back() -> None:
    above = _run(4000)
    assert above["passed_floor"] is True
    assert above["matched_features"], "two shared features are above the floor of 4000"

    below = _run(8000)
    assert below["passed_floor"] is False
    assert below["matched_features"] == [], "below the declared floor nothing is returned"
    assert below["overlap_basis_points"] == above["overlap_basis_points"], (
        "the floor changes what is returned, not what was computed"
    )


def test_every_step_carries_a_signed_envelope() -> None:
    result = _run()
    assert result["evidence"], "the exchange must leave evidence behind"
    for name, envelope in result["evidence"].items():
        assert envelope["evidence_id"].startswith("ev_"), name
        assert envelope["signature_alg"] == "ES256", name
        assert envelope["limitations"], f"{name} must state its limits"
