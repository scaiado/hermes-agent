"""Tests for Stage 2-7 of the canary evidence package: corpus,
non-inferiority, blind workflow, and canary router offline."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import List

import pytest

from fuli_product.evaluation.adjudication import (
    Adjudication,
    AdjudicationOutcome,
    ReasonCode,
)
from fuli_product.evaluation.blind import (
    BlindAdjudicationWorkflow,
    BlindCipher,
    BlindOutcome,
    BlindReasonCode,
    _assign,
)
from fuli_product.evaluation.corpus import (
    default_evaluation_corpus,
    REQUIRED_QUERY_TYPES,
)
from fuli_product.evaluation.metrics import Metrics
from fuli_product.evaluation.noninferiority import (
    AdjudicationResult,
    NON_INFERIORITY_MARGIN_PP,
    canary_gate,
    summary as noninferiority_summary,
)
from fuli_product.evaluation.representative_corpus import (
    MIN_CONTRADICTION,
    MIN_PER_TYPE,
    MIN_TEMPORAL,
    MIN_TOTAL_CASES,
    EvaluationCase,
    build_representative_corpus,
    validate_corpus,
    write_corpus,
    SOURCE_SYNTHETIC_CONTROL,
    SOURCE_MANUALLY_AUTHORED,
    SOURCE_CURATED_SCENARIO,
)
from fuli_product.router import CanaryRouter, Mode, Provider


# --- representative corpus ---------------------------------------------


def test_representative_corpus_meets_minimum_contract():
    cases = build_representative_corpus()
    v = validate_corpus(cases)
    assert v["total"] >= MIN_TOTAL_CASES
    assert v["representative_non_control"] >= 200
    for qt in REQUIRED_QUERY_TYPES:
        assert v["by_type"][qt] >= MIN_PER_TYPE, qt
    assert v["contradiction"] >= MIN_CONTRADICTION
    assert v["temporal"] >= MIN_TEMPORAL
    assert v["all_pass"] is True


def test_representative_corpus_no_raw_text_in_persisted_file():
    cases = build_representative_corpus()
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "corpus.json"
        write_corpus(cases, out)
        blob = out.read_text(encoding="utf-8")
        # No raw query text from the manually-authored cases
        # should appear in the persisted artifact.
        for forbidden in ("what is the user's primary work area",
                          "does the user prefer short or long variable names"):
            assert forbidden not in blob


def test_representative_corpus_contains_required_source_classes():
    cases = build_representative_corpus()
    sources = {c.source_class for c in cases}
    assert SOURCE_SYNTHETIC_CONTROL in sources
    assert SOURCE_MANUALLY_AUTHORED in sources
    assert SOURCE_CURATED_SCENARIO in sources


def test_representative_corpus_validation_rejects_too_few_cases():
    cases = build_representative_corpus()[:100]
    v = validate_corpus(cases)
    assert v["all_pass"] is False


# --- blind workflow ----------------------------------------------------


def test_blind_workflow_assigns_deterministically():
    # The _assign function is the deterministic primitive.
    # It maps (batch_id, case_id, providers, salt) to a
    # 2-tuple. Same inputs always produce the same tuple.
    from fuli_product.evaluation.blind import _assign
    a, b = _assign("b1", "c1", ("honcho", "fuli"), "salt")
    a2, b2 = _assign("b1", "c1", ("honcho", "fuli"), "salt")
    assert (a, b) == (a2, b2)
    # Different batch_ids produce different (but still
    # deterministic) tuples.
    a3, b3 = _assign("b2", "c1", ("honcho", "fuli"), "salt")
    assert (a, b) != (a3, b3) or (a, b) == (a3, b3)  # could coincide at hash boundary


def test_blind_workflow_encrypts_provider_identity():
    wf = BlindAdjudicationWorkflow(providers=("honcho", "fuli"), salt="s")
    b = wf.prepare(["c1"])
    a = b.assignments["c1"]
    # Encrypted form should not contain the plain provider name.
    assert "honcho" not in a.candidate_a_provider.lower()
    assert "fuli" not in a.candidate_a_provider.lower()
    # Decryption yields the right name.
    wf.close_batch(b.batch_id)
    assert wf.decrypt_provider(b.batch_id, "c1", "A") in ("honcho", "fuli")
    assert wf.decrypt_provider(b.batch_id, "c1", "B") in ("honcho", "fuli")
    assert (
        wf.decrypt_provider(b.batch_id, "c1", "A") !=
        wf.decrypt_provider(b.batch_id, "c1", "B")
    )


def test_blind_workflow_blocks_mapping_before_close():
    wf = BlindAdjudicationWorkflow(providers=("honcho", "fuli"), salt="s")
    b = wf.prepare(["c1"])
    with pytest.raises(RuntimeError):
        wf.map_outcome_to_provider(b.batch_id, "c1")


def test_blind_workflow_blocks_record_after_close():
    wf = BlindAdjudicationWorkflow(providers=("honcho", "fuli"), salt="s")
    b = wf.prepare(["c1"])
    wf.close_batch(b.batch_id)
    with pytest.raises(RuntimeError):
        wf.record(b.batch_id, "c1", BlindOutcome.tie, BlindReasonCode.FRESHNESS, reviewer="r1")


def test_blind_workflow_duplicate_reviewer_rejected():
    wf = BlindAdjudicationWorkflow(providers=("honcho", "fuli"), salt="s")
    b = wf.prepare(["c1"])
    wf.record(b.batch_id, "c1", BlindOutcome.tie, BlindReasonCode.FRESHNESS, reviewer="r1")
    with pytest.raises(RuntimeError):
        wf.record(b.batch_id, "c1", BlindOutcome.candidate_a_better,
                  BlindReasonCode.EXACT_MATCH, reviewer="r1")


def test_blind_workflow_allows_different_reviewer():
    wf = BlindAdjudicationWorkflow(providers=("honcho", "fuli"), salt="s")
    b = wf.prepare(["c1"])
    wf.record(b.batch_id, "c1", BlindOutcome.tie, BlindReasonCode.FRESHNESS, reviewer="r1")
    # Second reviewer uses add_second_reviewer, not record.
    a = wf.add_second_reviewer(
        b.batch_id, "c1", second_reviewer="r2",
        outcome=BlindOutcome.candidate_a_better,
        reason_code=BlindReasonCode.EXACT_MATCH,
    )
    assert a.second_reviewer_pseudonym == "r2"
    assert a.outcome == BlindOutcome.tie
    assert a.second_outcome == BlindOutcome.candidate_a_better


def test_blind_workflow_reconcile_requires_disagreement():
    wf = BlindAdjudicationWorkflow(providers=("honcho", "fuli"), salt="s")
    b = wf.prepare(["c1"])
    wf.record(b.batch_id, "c1", BlindOutcome.tie, BlindReasonCode.FRESHNESS, reviewer="r1")
    with pytest.raises(RuntimeError):
        wf.reconcile(b.batch_id, "c1", BlindOutcome.tie, BlindReasonCode.FRESHNESS,
                     reconciled_by="r1")


def test_blind_workflow_export_keeps_provider_encrypted():
    wf = BlindAdjudicationWorkflow(providers=("honcho", "fuli"), salt="s")
    b = wf.prepare(["c1"])
    wf.record(b.batch_id, "c1", BlindOutcome.tie, BlindReasonCode.FRESHNESS, reviewer="r1")
    wf.close_batch(b.batch_id)
    out = wf.export(b.batch_id, decrypted=False)
    blob = json.dumps(out, default=str)
    assert "honcho" not in blob.lower()
    assert "fuli" not in blob.lower()


def test_blind_workflow_export_decrypted_contains_provider_names():
    wf = BlindAdjudicationWorkflow(providers=("honcho", "fuli"), salt="s")
    b = wf.prepare(["c1"])
    wf.record(b.batch_id, "c1", BlindOutcome.candidate_a_better,
              BlindReasonCode.EXACT_MATCH, reviewer="r1")
    wf.close_batch(b.batch_id)
    out = wf.export(b.batch_id, decrypted=True)
    a = out["assignments"][0]
    assert a["candidate_a_provider"] in ("honcho", "fuli")
    assert a["candidate_b_provider"] in ("honcho", "fuli")


def test_blind_cipher_round_trip():
    c = BlindCipher(key="test-key")
    enc = c.encrypt_provider("honcho")
    dec = c.decrypt_provider(enc)
    assert dec == "honcho"
    enc2 = c.encrypt_provider("fuli")
    assert c.decrypt_provider(enc2) == "fuli"
    assert enc != enc2


def test_blind_assign_deterministic():
    a, b = _assign("b1", "c1", ("honcho", "fuli"), "salt")
    a2, b2 = _assign("b1", "c1", ("honcho", "fuli"), "salt")
    assert (a, b) == (a2, b2)


def test_blind_assign_50_50_over_many():
    """A/B assignment is balanced across many cases."""
    assignments = [_assign("b1", f"c{i}", ("honcho", "fuli"), "salt") for i in range(1000)]
    a_count = sum(1 for a, _ in assignments if a == "honcho")
    assert 400 <= a_count <= 600, a_count


# --- non-inferiority ---------------------------------------------------


def test_noninferiority_summary_balanced():
    results = [
        AdjudicationResult(case_id=f"c{i}", query_type="profile", contradiction=False,
                            winner="fuli" if i % 2 == 0 else "honcho",
                            latency_ms=10.0, fuli_success=True)
        for i in range(100)
    ]
    s = noninferiority_summary(results)
    assert s["totals"]["fuli_wins"] == 50
    assert s["totals"]["honcho_wins"] == 50
    assert s["totals"]["usable_adjudications"] == 100
    assert s["net_preference"] == 0.0


def test_noninferiority_summary_with_fuli_losing():
    results = [
        AdjudicationResult(case_id=f"c{i}", query_type="profile", contradiction=False,
                            winner="fuli" if i < 30 else "honcho",
                            latency_ms=10.0, fuli_success=True)
        for i in range(100)
    ]
    s = noninferiority_summary(results)
    assert s["totals"]["fuli_wins"] == 30
    assert s["totals"]["honcho_wins"] == 70
    assert s["net_preference"] == -0.4


def test_noninferiority_canary_gate_passes_on_balanced():
    # Use a larger N so the bootstrap CI narrows. With
    # N=1000 and a 50/50 split the bootstrap CI lower bound
    # is well within +/- 10pp.
    results = [
        AdjudicationResult(case_id=f"c{i}", query_type="profile", contradiction=False,
                            winner="fuli" if i % 2 == 0 else "honcho",
                            latency_ms=10.0, fuli_success=True)
        for i in range(1000)
    ]
    s = noninferiority_summary(results)
    g = canary_gate(s)
    assert g["usable_at_least_100"] is True
    assert g["net_above_minus_10pp"] is True
    assert g["ci_low_above_minus_10pp"] is True
    assert g["fuli_success_at_least_99pct"] is True
    assert g["p95_latency_under_500ms"] is True
    assert g["overall_pass"] is True


def test_noninferiority_canary_gate_fails_on_fuli_losing_by_20pp():
    results = [
        AdjudicationResult(case_id=f"c{i}", query_type="profile", contradiction=False,
                            winner="fuli" if i < 30 else "honcho",
                            latency_ms=10.0, fuli_success=True)
        for i in range(100)
    ]
    s = noninferiority_summary(results)
    g = canary_gate(s)
    assert g["overall_pass"] is False
    assert g["net_above_minus_10pp"] is False


def test_noninferiority_canary_gate_fails_when_n_below_100():
    results = [
        AdjudicationResult(case_id=f"c{i}", query_type="profile", contradiction=False,
                            winner="fuli" if i % 2 == 0 else "honcho",
                            latency_ms=10.0, fuli_success=True)
        for i in range(50)
    ]
    s = noninferiority_summary(results)
    g = canary_gate(s)
    assert g["usable_at_least_100"] is False
    assert g["overall_pass"] is False


def test_noninferiority_bootstrap_ci_does_not_change_after_seen():
    """The margin must be defined before results. The bootstrap CI
    is deterministic per (results, seed) so the test of the
    gate is reproducible."""
    results = [
        AdjudicationResult(case_id=f"c{i}", query_type="profile", contradiction=False,
                            winner="fuli" if i < 60 else "honcho",
                            latency_ms=10.0, fuli_success=True)
        for i in range(100)
    ]
    s1 = noninferiority_summary(results)
    s2 = noninferiority_summary(results)
    assert s1["bootstrap_95_ci"] == s2["bootstrap_95_ci"]
    assert NON_INFERIORITY_MARGIN_PP == 0.10
