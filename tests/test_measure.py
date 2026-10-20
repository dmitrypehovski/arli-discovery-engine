"""The measurer: numbers are derived from envelopes, and the unmeasured is honestly named unmeasured.

The main check here is one and it comes from the plan: quantities are computed from
envelopes, not from a separate counter that can be edited by hand. So the envelopes are
real, signed by a live agent, and the test shows that swapping the set of envelopes changes
the number.
"""

from __future__ import annotations

import pytest

from arli_engine.adapters.a2a.cards import build_card
from arli_engine.adapters.a2a.server import AgentState, start_agent
from arli_engine.client import AgentClient
from arli_engine.core.capability import EFFECT_KEYS, Operation
from arli_engine.core.match import FeatureSet
from arli_engine.core.signing import KeyPair
from arli_engine.measure import (
    KNOWN_REFUSAL_CODES,
    MEASURED,
    MEASURE_REJECTIONS,
    NOT_MEASURED,
    MeasureRejected,
    closed_intents_share,
    compute_all,
    envelope_is_evidence,
    evidence_coverage,
    false_positive_share,
    matches_above_floor,
    measure,
    measured,
    not_measured,
    refusal_code_completeness,
    time_to_decision_median,
)


def _agent(port: int = 0) -> tuple[AgentState, KeyPair, object]:
    key = KeyPair.generate()
    every = {key_name: False for key_name in EFFECT_KEYS}
    operations = [Operation(name="discover.match", description="Match intents", effects=every)]
    card = build_card(
        name="arli-measured",
        description="agent under measurement",
        base_url="http://127.0.0.1:0",
        skills=[{"id": "discover.match", "name": "match", "description": "d",
                 "inputModes": ["application/json"], "outputModes": ["application/json"]}],
        operations=operations,
        public_features=["skill:design"],
        kid="arli-measured-key-1",
        private_pem=key.private_pem,
    )
    state = AgentState(
        "arli-measured", card, operations, FeatureSet(["skill:design"]), "eu-central",
        private_pem=key.private_pem, kid="arli-measured-key-1",
    )
    server, _thread = start_agent(state, port)
    return state, key, server


def _signed_envelope(server) -> dict:
    """A real signed envelope from a live agent."""
    port = server.server_address[1]
    with AgentClient(f"http://127.0.0.1:{port}") as client:
        client.arli_retention_record({"evidence_id": "ev-1", "intent_id": "intent-1"})
        archived = client.arli_retention_archive({"intent_id": "intent-1"})
    return archived["evidence"]


def test_a_real_signed_envelope_counts_as_evidence():
    _state, key, server = _agent()
    try:
        envelope = _signed_envelope(server)
        assert envelope["signature"] is not None
        assert envelope_is_evidence(envelope, key.public_pem) is True
        # without the issuer key the envelope does not count as evidence
        assert envelope_is_evidence(envelope, None) is False
        # with someone else's key neither
        assert envelope_is_evidence(envelope, KeyPair.generate().public_pem) is False
    finally:
        server.shutdown()
        server.server_close()


def test_a_tampered_envelope_is_not_evidence():
    _state, key, server = _agent()
    try:
        envelope = _signed_envelope(server)
        tampered = dict(envelope)
        tampered["normalized_result"] = dict(tampered["normalized_result"], marked_deleted=99)
        assert envelope_is_evidence(tampered, key.public_pem) is False
    finally:
        server.shutdown()
        server.server_close()


def test_coverage_is_computed_from_the_envelopes_not_from_a_counter():
    _state, key, server = _agent()
    try:
        good = _signed_envelope(server)
        keys = {"arli-retention": key.public_pem}
        assert evidence_coverage([good], keys).value == 1.0
        broken = dict(good)
        broken.pop("signature")
        assert evidence_coverage([good, broken], keys).value == 0.5
        assert evidence_coverage([broken, broken], keys).value == 0.0
        # and for an issuer without a key the coverage drops: a signature without a key is not verification
        assert evidence_coverage([good], {}).value == 0.0
    finally:
        server.shutdown()
        server.server_close()


def test_refusal_completeness_separates_codes_from_free_text():
    refusals = [
        {"code": "missing_floor"},
        {"code": "types_not_comparable"},
        {"code": "because-i-said-so"},
        {"code": None},
    ]
    metric = refusal_code_completeness(refusals)
    assert metric.value == 0.5
    assert "missing_floor" in KNOWN_REFUSAL_CODES and "types_not_comparable" in KNOWN_REFUSAL_CODES


def test_the_other_metrics_are_plain_ratios_over_their_inputs():
    assert closed_intents_share([{"closed": True}, {"closed": False}]).value == 0.5
    assert matches_above_floor([{"passed_floor": True}, {"passed_floor": True}]).value == 1.0
    assert time_to_decision_median([{"seconds_to_decision": 10}, {"seconds_to_decision": 30}]).value == 20.0
    assert false_positive_share([{"junk": True}, {"junk": False}, {"junk": False}]).value == 1 / 3


def test_an_empty_record_reports_everything_as_not_measured_with_a_reason():
    report = measure({}, source="empty")
    assert len(report.metrics) == 7
    assert report.measured_count == 0
    assert all(metric.state == NOT_MEASURED and metric.reason for metric in report.metrics)
    # zero instead of "we don't know" is the worst kind of lie, so there is no value here
    assert all(metric.value is None for metric in report.metrics)


def test_human_labels_are_never_guessed():
    metric = false_positive_share([])
    assert metric.state == NOT_MEASURED
    assert "does not guess them" in metric.reason


def test_the_report_renders_a_table_and_json():
    record = {
        "refusals": [{"code": "missing_floor"}],
        "intents": [{"closed": True}, {"closed": False}],
        "matches": [{"passed_floor": True}],
    }
    report = measure(record, source="unit")
    table = report.as_table()
    assert "refusal_code_completeness: 1.0000 share" in table
    assert "false_positive_share: not measured" in table
    payload = report.to_json()
    # three are measured: refusals, intents, finds. Four are not, and that is said out loud.
    assert payload["measured"] == 3 and payload["not_measured"] == 4
    assert payload["source"] == "unit"


def test_metric_names_and_states_are_closed_sets():
    assert MEASURED == "measured" and NOT_MEASURED == "not_measured"
    with pytest.raises(ValueError):
        MeasureRejected("not_a_real_code")
    assert "unknown_metric" in MEASURE_REJECTIONS
    assert measured("x", 1.0, unit="share", basis="b").value == 1.0
    assert not_measured("x", basis="b", reason="r").value is None


def test_the_calibration_run_says_what_it_is_and_what_it_is_not():
    """A run over synthetic data must call itself calibration, not pilot numbers."""
    import importlib.util
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("measure_dry_run", root / "tools" / "measure_dry_run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main() == 0
    payload = json.loads(Path(module.OUT).read_text(encoding="utf-8"))
    assert "not a pilot" in payload["source"] or "not a pilot" in payload["caveat"]
    assert "must not be presented as a funnel" in payload["caveat"]
    names = {metric["name"] for metric in payload["metrics"]}
    assert len(names) == 7
    # a synthetic run cannot measure human quantities, and does not pretend to
    by_name = {metric["name"]: metric for metric in payload["metrics"]}
    assert by_name["false_positive_share"]["state"] == "not_measured"
    assert by_name["evidence_coverage"]["value"] == 1.0
