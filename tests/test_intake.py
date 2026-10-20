"""The intake: a person's words, and what is done with them.

The door for somebody who has no agent, no server and no vocabulary. What matters is not that it reads well,
but that it never invents: a word it cannot place must come back to the person, an ambiguous word must be
offered as a choice rather than decided, and a phrase must never match inside a longer word.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arli_engine.core.intake import Taxonomy, read_request  # noqa: E402


@pytest.fixture()
def taxonomy() -> Taxonomy:
    return Taxonomy.built_in()


def test_a_written_need_becomes_features_in_the_need_namespace(taxonomy: Taxonomy) -> None:
    read = read_request("We need warehousing and customs clearance near shenzhen, 3 teams, gdpr", taxonomy=taxonomy)
    assert read["kind"] == "demand"
    assert "need:warehousing" in read["features"]
    assert "need:customs-clearance" in read["features"]
    assert "city:shenzhen" in read["features"]
    assert "compliance:gdpr" in read["features"]
    assert "capacity:3-teams" in read["features"]


def test_the_same_words_from_a_supplier_are_an_offer_not_a_need(taxonomy: Taxonomy) -> None:
    read = read_request("We offer warehousing, based in berlin", taxonomy=taxonomy)
    assert read["kind"] == "supply"
    assert "offer:warehousing" in read["features"]
    assert not any(feature.startswith("need:") for feature in read["features"]), (
        "the same words mean the opposite thing from the other side, so the namespace must follow the side"
    )


def test_a_phrase_never_matches_inside_a_longer_word(taxonomy: Taxonomy) -> None:
    """The first version of the intake read the certificate "ul" out of the word "fulfilment"."""
    read = read_request("We need fulfilment", taxonomy=taxonomy)
    assert not any(feature.startswith("compliance:") for feature in read["features"]), read["features"]
    assert "fulfilment" in read["not_understood"] or "need:warehousing" in read["features"], (
        "either it is placed or it is reported, and it is not turned into a certificate"
    )


def test_a_word_fitting_several_entries_goes_back_to_the_person(taxonomy: Taxonomy) -> None:
    read = read_request("We need contract work, based in london", taxonomy=Taxonomy(entries={
        "contracts": {"title": "Contracts", "category": "law-regulation-compliance"},
        "contract-negotiation": {"title": "Contract Negotiation", "category": "sales"},
        "employment-contracts": {"title": "Employment Contracts", "category": "human-resources"},
    }))
    assert read["ambiguous"], "three entries share the stem, so the choice is the person's"
    offered = {candidate["slug"] for item in read["ambiguous"] for candidate in item["could_be"]}
    assert {"contracts", "contract-negotiation", "employment-contracts"} <= offered
    assert not any(feature.split(":", 1)[-1] in offered for feature in read["features"]), (
        "an ambiguous word must not be decided silently"
    )
    assert any("fits more than one entry" in question for question in read["questions"])


def test_a_property_is_not_turned_into_a_service(taxonomy: Taxonomy) -> None:
    read = read_request("I offer brand identity for retail clients in berlin", taxonomy=taxonomy)
    assert "industry:retail" in read["features"], "a sector is a property, not something on offer"
    assert "city:berlin" in read["features"]
    assert not any(feature.split(":", 1)[-1] == "retail" for feature in read["features"]
                   if feature.startswith(("need:", "offer:")))


def test_a_bucket_is_the_taxonomy_category_so_the_agent_lands_in_the_shared_index(taxonomy: Taxonomy) -> None:
    read = read_request("We need warehousing and customs clearance in shenzhen", taxonomy=taxonomy)
    assert read["buckets"] == {"transportation-supply-chain-logistics": 2}, read["buckets"]


def test_words_that_could_not_be_placed_are_returned_not_dropped(taxonomy: Taxonomy) -> None:
    read = read_request("We need warehousing and a very particular arrangement of moonlight", taxonomy=taxonomy)
    assert "moonlight" in read["not_understood"], read["not_understood"]
    assert any("were not placed" in question for question in read["questions"])


def test_nothing_placeable_means_no_features_and_a_question(taxonomy: Taxonomy) -> None:
    read = read_request("Hello, I would like something good please", taxonomy=taxonomy)
    assert read["features"] == []
    assert read["questions"], "an empty reading must ask rather than produce an empty agent"


def test_the_same_sentence_read_twice_gives_the_same_features(taxonomy: Taxonomy) -> None:
    """Deterministic, or the same person gets two different agents for the same sentence."""
    first = read_request("We need warehousing in shenzhen, 3 teams, gdpr", taxonomy=taxonomy)
    second = read_request("We need warehousing in shenzhen, 3 teams, gdpr", taxonomy=taxonomy)
    assert first["features"] == second["features"]
    assert first["buckets"] == second["buckets"]


def test_the_production_taxonomy_loads_and_names_a_category(taxonomy: Taxonomy) -> None:
    production = Path("/llm-gateway/skill_taxonomy_v2.json")
    if not production.is_file():
        pytest.skip("production taxonomy not present on this machine")
    loaded = Taxonomy.from_file(production)
    assert loaded.size() > 300, "the production file is the one with hundreds of entries"
    read = read_request("We need customs clearance near shenzhen", taxonomy=loaded)
    assert "need:customs-clearance" in read["features"], read["features"]
    assert read["buckets"].get("transportation-supply-chain-logistics"), read["buckets"]