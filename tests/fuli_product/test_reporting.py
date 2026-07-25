"""Stable schema tests for the Reporter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Set

import pytest

from fuli_product.adapters import InMemoryConfigRepository
from fuli_product.reporting import (
    Reporter,
    ReporterError,
    SCHEMA_VERSION,
    validate_period,
)

from ._fakes import FakeComparisonRuntime

SHADOW_CONFIG = {
    "memory": {
        "fuli_product": {
            "mode": "shadow",
            "previous_mode": "",
            "compare_reads": True,
            "sample_rate": 1.0,
        },
    },
}

REQUIRED_TOP_LEVEL: Set[str] = {
    "schema_version",
    "product_version",
    "state",
    "mode",
    "previous_mode",
    "health",
    "hard_gates",
    "provider_health",
    "executor_accounting",
    "comparison_metrics",
    "privacy",
    "database_integrity",
    "compatibility",
    "generated_at",
}


def _reporter(runtime):
    repo = InMemoryConfigRepository(SHADOW_CONFIG)
    return Reporter(repo, hermes_home=Path("/tmp/fuli_test"), runtime=runtime)


def test_status_returns_stable_schema():
    runtime = FakeComparisonRuntime(accounting_value={"primary_mutation": 1})
    reporter = _reporter(runtime)
    status = reporter.status()
    assert status["schema_version"] == SCHEMA_VERSION
    assert set(status.keys()) >= REQUIRED_TOP_LEVEL
    assert isinstance(status["hard_gates"], list)
    assert "primary_mutation" in status["hard_gates"]
    assert status["executor_accounting"]["primary_mutation"] == 1
    assert "primary_mutations" not in status["executor_accounting"]
    assert "persistence_failed" not in status["executor_accounting"]
    assert status["mode"] == "shadow"
    assert status["state"] in {"healthy", "degraded", "auto_paused", "paused", "incompatible", "unavailable"}


def test_status_with_runtime_unavailable_is_safe():
    reporter = _reporter(FakeComparisonRuntime())
    status = reporter.status()
    assert status["executor_accounting"]["primary_mutation"] == 0
    assert status["state"] in {"healthy", "degraded", "auto_paused", "unavailable", "incompatible"}


def test_status_accounting_exception_produces_safe_envelope():
    runtime = FakeComparisonRuntime(raise_on_accounting=True)
    reporter = _reporter(runtime)
    status = reporter.status()
    assert status["state"] == "unavailable"
    assert any("accounting_unavailable" in r for r in status["health"]["reasons"])


def test_status_is_read_only():
    repo = InMemoryConfigRepository(SHADOW_CONFIG)
    runtime = FakeComparisonRuntime()
    reporter = Reporter(repo, hermes_home=Path("/tmp/fuli_test"), runtime=runtime)
    before = repo.read_dict()
    reporter.status()
    after = repo.read_dict()
    assert before == after


def test_status_uses_only_canonical_accounting_keys():
    runtime = FakeComparisonRuntime(accounting_value={
        "primary_mutations": 1,
        "namespace_violations": 1,
        "dropped_queue_full": 1,
        "persistence_failed": 1,
        "completed": 5,
    })
    reporter = _reporter(runtime)
    status = reporter.status()
    canonical = set(status["executor_accounting"].keys())
    for legacy in ("primary_mutations", "namespace_violations", "dropped_queue_full", "persistence_failed"):
        assert legacy not in canonical
    for required in ("primary_mutation", "namespace_violation", "queue_drop", "persistence_failure"):
        assert required in canonical


def test_report_returns_documented_schema():
    runtime = FakeComparisonRuntime()
    reporter = _reporter(runtime)
    report = reporter.report("24h")
    assert report["schema_version"] == SCHEMA_VERSION
    assert set(report.keys()) >= REQUIRED_TOP_LEVEL
    assert report["period"] == "24h"


def test_report_validates_period():
    runtime = FakeComparisonRuntime()
    reporter = _reporter(runtime)
    with pytest.raises(ReporterError):
        reporter.report("definitely-not-a-period")


def test_validate_period_accepts_supported_patterns():
    for good in ("1h", "6h", "24h", "7d", "30d", "12h", "5d"):
        validate_period(good)


def test_validate_period_rejects_garbage():
    with pytest.raises(ReporterError):
        validate_period("eod")
    with pytest.raises(ReporterError):
        validate_period("")
    with pytest.raises(ReporterError):
        validate_period("24")


def test_export_produces_redacted_bundle(tmp_path: Path):
    runtime = FakeComparisonRuntime()
    reporter = _reporter(runtime)
    out = reporter.export(redacted=True, output=tmp_path / "export.json")
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert "status" in payload and "report" in payload
    json.dumps(payload, default=str)


def test_status_contains_no_secrets_or_raw_queries():
    runtime = FakeComparisonRuntime()
    reporter = _reporter(runtime)
    payload = json.dumps(reporter.status(), default=str)
    for bad in ("secret", "password", "api_key"):
        assert bad not in payload.lower()
    status = reporter.status()
    for field in ("query", "raw_query", "payload", "secret", "api_key"):
        assert field not in status
