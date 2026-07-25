"""Canonical hard-gate schema and accounting normalization."""

from __future__ import annotations

from fuli_product.config import (
    CANONICAL_HARD_GATE_KEYS,
    LEGACY_HARD_GATE_KEYS,
    RollbackConfig,
    normalize_accounting,
    normalize_hard_gates,
)


def test_canonical_keys_set():
    assert CANONICAL_HARD_GATE_KEYS == frozenset({
        "primary_mutation",
        "namespace_violation",
        "raw_content_leak",
        "persistence_failure",
        "queue_drop",
        "orphaned_worker",
        "unexpected_collision",
        "sqlite_integrity_failure",
        "secondary_success_below_threshold",
    })


def test_normalize_hard_gates_maps_legacy_keys():
    out = normalize_hard_gates([
        "primary_mutations",
        "namespace_violations",
        "persistence_failed",
        "queue_drops",
        "orphaned",
        "secondary_success_rate_low",
    ])
    assert out == [
        "primary_mutation",
        "namespace_violation",
        "persistence_failure",
        "queue_drop",
        "orphaned_worker",
        "secondary_success_below_threshold",
    ]


def test_normalize_hard_gates_drops_unknown_and_dedupes():
    out = normalize_hard_gates([
        "primary_mutation", "primary_mutation", "primary_mutations", "bogus_key",
    ])
    assert out == ["primary_mutation"]


def test_normalize_accounting_folds_legacy_into_canonical():
    out = normalize_accounting({
        "primary_mutations": 1,
        "namespace_violation": 2,
        "dropped_queue_full": 3,
        "queue_drops": 4,
        "completed": 7,
    })
    assert out["primary_mutation"] == 1
    assert out["namespace_violation"] == 2
    assert out["queue_drop"] == 7
    assert out["completed"] == 7


def test_rollback_config_normalizes_user_input():
    rc = RollbackConfig(auto_rollback_on=["primary_mutations", "queue_drops"])
    assert rc.auto_rollback_on == ["primary_mutation", "queue_drop"]


def test_legacy_map_is_complete():
    for legacy in {
        "primary_mutations", "namespace_violations", "persistence_failed",
        "queue_drops", "dropped_queue_full", "orphaned",
        "secondary_success_rate_low",
    }:
        assert legacy in LEGACY_HARD_GATE_KEYS, legacy
