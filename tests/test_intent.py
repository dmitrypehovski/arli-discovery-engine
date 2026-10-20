"""Intent types, provenance and their effect on pair eligibility.

Here we check what makes the unit of work a closed intent rather than an
introduction: demand matches with supply, two supplies do not match each other,
and a synthetic intent does not enter a human network.
"""

from __future__ import annotations

import pytest

from arli_engine.core.intent import (
    INTENT_REJECTIONS,
    Intent,
    IntentRejected,
    check_admission,
    types_comparable,
)
from arli_engine.core.negotiate import OPENING_REJECTIONS, IntentStanding, decide_opening


def test_supply_meets_demand_and_people_meet_people():
    assert types_comparable("supply", "demand") is True
    assert types_comparable("demand", "supply") is True
    assert types_comparable("person", "person") is True


def test_two_supplies_or_two_demands_are_not_a_match():
    assert types_comparable("supply", "supply") is False
    assert types_comparable("demand", "demand") is False
    assert types_comparable("person", "supply") is False


def test_unknown_type_is_refused_with_a_code():
    with pytest.raises(IntentRejected) as exc:
        types_comparable("barrel", "demand")
    assert exc.value.code == "unknown_type"


def test_intent_validation_covers_all_three_fields():
    with pytest.raises(IntentRejected) as exc:
        Intent(intent_id="i", owner="o", network="n", type="weird").validate()
    assert exc.value.code == "unknown_type"
    with pytest.raises(IntentRejected) as exc:
        Intent(intent_id="i", owner="o", network="n", status="ghost").validate()
    assert exc.value.code == "unknown_status"
    with pytest.raises(IntentRejected) as exc:
        Intent(intent_id="i", owner="o", network="n", provenance="robot").validate()
    assert exc.value.code == "unknown_provenance"


def test_synthetic_is_refused_in_a_human_network_and_admitted_when_declared():
    synthetic = Intent(intent_id="i", owner="o", network="n", provenance="synthetic")
    with pytest.raises(IntentRejected) as exc:
        check_admission(synthetic, admits_synthetic=False)
    assert exc.value.code == "synthetic_not_admitted"
    check_admission(synthetic, admits_synthetic=True)  # passes without an exception

    human = Intent(intent_id="i", owner="o", network="n")
    check_admission(human, admits_synthetic=False)


def test_inactive_intent_is_refused():
    paused = Intent(intent_id="i", owner="o", network="n", status="paused")
    with pytest.raises(IntentRejected) as exc:
        check_admission(paused, admits_synthetic=False)
    assert exc.value.code == "intent_inactive"


def test_pair_of_supplies_does_not_open_but_supply_with_demand_does():
    supply = IntentStanding(intent_id="i1", owner="a", network="n1", type="supply")
    demand = IntentStanding(intent_id="i2", owner="b", network="n1", type="demand")
    another_supply = IntentStanding(intent_id="i3", owner="b", network="n1", type="supply")
    assert decide_opening(supply, demand, "n1") is None
    assert decide_opening(supply, another_supply, "n1") == "types_not_comparable"


def test_synthetic_pair_does_not_open_in_a_human_network():
    synthetic = IntentStanding(intent_id="i1", owner="a", network="n1", provenance="synthetic")
    human = IntentStanding(intent_id="i2", owner="b", network="n1")
    assert decide_opening(synthetic, human, "n1") == "synthetic_not_admitted"
    assert decide_opening(synthetic, human, "n1", admits_synthetic=True) is None


def test_intent_rejections_are_a_closed_set():
    with pytest.raises(ValueError):
        IntentRejected("not_a_real_code")
    assert "synthetic_not_admitted" in INTENT_REJECTIONS
    assert "types_not_comparable" in INTENT_REJECTIONS
    # the pair eligibility codes and the intent codes overlap meaningfully: both sets are closed
    assert set(INTENT_REJECTIONS) & set(OPENING_REJECTIONS) == {
        "intent_inactive", "synthetic_not_admitted", "types_not_comparable", "network_mismatch",
    }


def test_which_pairings_the_network_allows_today() -> None:
    """The matrix, written down so a change to it is a decision and not a drift.

    Opposite directions match: a buyer and a seller. People match with people. Two sellers are competition
    and two buyers are a queue, so neither is a pair. A person and a company are not a pair either, because
    the type says what a side IS rather than which way it points: a freelancer who offers work declares
    supply, and a client who needs work declares demand.
    """
    from arli_engine.core.intent import types_comparable

    assert types_comparable("demand", "supply") is True
    assert types_comparable("supply", "demand") is True
    assert types_comparable("person", "person") is True
    for first, second in (("supply", "supply"), ("demand", "demand"),
                          ("person", "supply"), ("person", "demand")):
        assert types_comparable(first, second) is False, f"{first} and {second} must not pair"


def test_a_person_offering_work_declares_supply_not_person() -> None:
    """The reading that keeps a freelancer matchable: the words decide the direction, not the human."""
    from arli_engine.core.intake import Taxonomy, read_request

    read = read_request("I offer translation into English, based in london", taxonomy=Taxonomy.built_in())
    assert read["kind"] == "supply", read
    # and somebody who needs that work declares demand, so the two are comparable
    other = read_request("We need a translator for legal documents, based in london",
                         taxonomy=Taxonomy.built_in())
    assert other["kind"] == "demand", other
