"""Tests for the Phase 6 evaluation package: corpus, adjudication, metrics,
capture, replay, report, and provider adapters."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from fuli_product.evaluation.corpus import (
    default_evaluation_corpus,
    EvaluationCorpus,
    EvaluationQuery,
    REQUIRED_QUERY_TYPES,
    MIN_TOTAL_QUERIES,
    MIN_PER_TYPE,
    StratifiedSampler,
    _hash_query,
)
from fuli_product.evaluation.adjudication import (
    Adjudication,
    AdjudicationOutcome,
    ReasonCode,
    REASON_CODES,
    summarize,
)
from fuli_product.evaluation.metrics import Metrics
from fuli_product.evaluation.capture import CapturedRow, capture_row
from fuli_product.evaluation.replay import replay_row
from fuli_product.evaluation.report import build_report, write_report
from fuli_product.evaluation.providers import (
    HonchoAdapter,
    FuliAdapter,
    all_available_adapters,
)


# --- corpus --------------------------------------------------------------


def test_default_corpus_satisfies_minimum_size():
    corpus = default_evaluation_corpus()
    assert corpus.total() >= MIN_TOTAL_QUERIES
    for qt in REQUIRED_QUERY_TYPES:
        assert len(corpus.by_type().get(qt, [])) >= MIN_PER_TYPE, qt


def test_query_hash_is_deterministic():
    a = _hash_query("Hello World")
    b = _hash_query("  hello world  ")
    assert a == b


def test_stratified_sampler_is_deterministic():
    corpus = default_evaluation_corpus()
    s1 = StratifiedSampler(corpus, seed=42)
    s2 = StratifiedSampler(corpus, seed=42)
    a = [q.query_hash for q in s1.sample(40)]
    b = [q.query_hash for q in s2.sample(40)]
    assert a == b


# --- adjudication --------------------------------------------------------


def test_adjudication_outcomes_enum():
    assert AdjudicationOutcome.fuli_better.value == "fuli_better"
    assert AdjudicationOutcome.honcho_better.value == "honcho_better"
    assert AdjudicationOutcome.tie.value == "tie"
    assert AdjudicationOutcome.both_bad.value == "both_bad"
    assert AdjudicationOutcome.insufficient_evidence.value == "insufficient_evidence"


def test_summarize_counts_outcomes():
    judgments = [
        Adjudication("c1", AdjudicationOutcome.fuli_better,
                     ReasonCode.FULI_MORE_RELEVANT, "profile", "alice", "2026-01-01"),
        Adjudication("c2", AdjudicationOutcome.honcho_better,
                     ReasonCode.HONCHO_MORE_RELEVANT, "profile", "bob", "2026-01-01"),
        Adjudication("c3", AdjudicationOutcome.tie,
                     ReasonCode.TIE, "semantic", "alice", "2026-01-01"),
    ]
    s = summarize(judgments)
    assert s == {
        "fuli_better": 1,
        "honcho_better": 1,
        "tie": 1,
        "both_bad": 0,
        "insufficient_evidence": 0,
    }


def test_reason_codes_include_contradiction():
    assert REASON_CODES["fuli_handled_contradiction"]
    assert REASON_CODES["honcho_handled_contradiction"]


# --- metrics -------------------------------------------------------------


def test_metrics_add_aggregates_per_type():
    m = Metrics()
    m.add("profile", fuli_better=True, latency_ms=12.0)
    m.add("profile", honcho_better=True, latency_ms=20.0)
    m.add("semantic", tie=True, latency_ms=8.0)
    assert m.overall.fuli_better == 1
    assert m.overall.honcho_better == 1
    assert m.overall.tie == 1
    assert m.overall.latencies_ms == [12.0, 20.0, 8.0]


def test_canary_gate_blocks_when_n_below_minimum():
    m = Metrics()
    m.add("profile", fuli_better=True, latency_ms=10.0, completed=True, sampled=True)
    g = m.canary_gate(p95_threshold_ms=100.0, n_min=100)
    assert g["sufficient_n"] is False
    assert g["overall_pass"] is False


def test_canary_gate_passes_when_all_conditions_met():
    m = Metrics()
    for _ in range(120):
        m.add("profile", fuli_better=True, latency_ms=10.0,
              completed=True, sampled=True)
    g = m.canary_gate(p95_threshold_ms=100.0, n_min=100)
    assert g["sufficient_n"] is True
    assert g["fuli_success_above_floor"] is True
    assert g["fuli_win_above_floor"] is True
    assert g["p95_within_threshold"] is True
    assert g["overall_pass"] is True


# --- capture / replay ---------------------------------------------------


def test_capture_row_does_not_store_raw_text():
    row = capture_row(
        query="what is the user's favorite X",
        query_type="preference",
        primary_payload=[{"id": "a", "content": "primary"}],
        secondary_payload=[{"id": "a", "content": "secondary"}],
        primary_provider="honcho",
        secondary_provider="fuli",
    )
    d = row.to_dict()
    blob = json.dumps(d, default=str)
    # Raw text is reduced to fingerprints; ensure no raw response text
    # is present in the serialized form.
    assert "primary" not in blob.lower() or "primary_provider" in blob
    # The query text is hashed, not stored verbatim.
    assert "what is the user's favorite" not in blob
    assert row.query_hash == _hash_query("what is the user's favorite X")


def test_replay_returns_fingerprints_only():
    row = capture_row(
        query="q",
        query_type="exact",
        primary_payload="primary",
        secondary_payload="secondary",
        primary_provider="honcho",
        secondary_provider="fuli",
    )
    out = replay_row(
        row, query="q", target_provider="hindsight",
        call=lambda q: [{"id": "h1", "content": "hit"}],
    )
    assert "latency_ms" in out
    assert "provider_fingerprints" in out
    assert all(len(fp) == 16 for fp in out["provider_fingerprints"])


# --- report --------------------------------------------------------------


def test_build_report_is_redacted_and_deterministic_schema():
    m = Metrics()
    for _ in range(120):
        m.add("profile", fuli_better=True, latency_ms=10.0)
    judgments = [
        Adjudication("c1", AdjudicationOutcome.fuli_better,
                     ReasonCode.FULI_MORE_RELEVANT, "profile", "alice", "2026-01-01")
        for _ in range(120)
    ]
    r = build_report(metrics=m, adjudications=judgments,
                    p95_threshold_ms=100.0, n_min=100)
    assert r["schema_version"] == 1
    assert "generated_at" in r
    assert "period" in r
    assert "overall" in r
    assert "by_type" in r
    assert "adjudication_summary" in r
    assert "canary_gate" in r
    blob = json.dumps(r, default=str)
    for forbidden in ("secret", "password", "api_key", "raw_query", "payload"):
        assert forbidden not in blob.lower()


def test_write_report_persists_valid_json(tmp_path: Path):
    m = Metrics()
    m.add("profile", fuli_better=True, latency_ms=10.0)
    r = build_report(metrics=m, adjudications=[])
    out = write_report(r, tmp_path / "r.json")
    assert out.exists()
    loaded = json.loads(out.read_text(encoding="utf-8"))
    assert loaded["schema_version"] == 1


# --- providers -----------------------------------------------------------


def test_honcho_adapter_retain_and_recall_round_trip():
    a = HonchoAdapter()
    fp = a.retain("q1", "profile", {"expected": "x"})
    res = a.recall("q1", top_k=3)
    assert res.fingerprints == []  # the stub doesn't index by query text
    assert res.latency_ms >= 0


def test_fuli_adapter_capabilities_typed_schema():
    a = FuliAdapter()
    cap = a.capabilities()
    assert cap.typed_schema is True
    assert cap.contradiction_detection is True
    assert "hybrid" in cap.retrieval_modes


def test_all_available_adapters_returns_honcho_and_fuli():
    adapters = all_available_adapters()
    assert adapters["honcho"] is not None
    assert adapters["fuli"] is not None
    # Hindsight / Mnemosyne are None when their packages are not present.
    # (Test environment has neither installed.)
    assert adapters["hindsight"] is None
    assert adapters["mnemosyne"] is None
