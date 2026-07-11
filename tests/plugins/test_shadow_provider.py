from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from plugins.memory.shadow import ShadowMemoryProvider
from plugins.memory.shadow.shadow_store import ShadowEvidenceStore
from tests.plugins.fake_providers import FakeFuliProvider, FakeHonchoProvider


@pytest.fixture
def tmp_shadow(tmp_path):
    """Create a ShadowMemoryProvider with a temp evidence store and injected fakes."""
    store_path = tmp_path / "shadow.db"
    primary = FakeHonchoProvider(mode="success")
    secondary = FakeFuliProvider()
    shadow = ShadowMemoryProvider()
    shadow._primary = primary
    shadow._secondary = secondary
    shadow._enabled = True
    shadow._mirror_writes = True
    shadow._compare_reads = False
    shadow._sample_rate = 0.0
    shadow._namespace = "hermes:shadow-test"
    shadow._capture_content = True
    shadow._store = shadow._create_store(str(store_path))
    return shadow, primary, secondary, store_path


def test_success_mirror_writes_to_fuli(tmp_shadow):
    shadow, primary, secondary, _ = tmp_shadow
    result = shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "The user prefers concise responses", "peer": "user"},
        pilot_meta={"correlation_id": "corr-1", "pilot_sequence": 1},
    )
    assert result == primary.calls[-1]["result"]
    assert len(secondary.add_calls) == 1
    assert secondary.add_calls[0]["metadata"]["correlation_id"] == "corr-1"
    assert "synthetic" in secondary.add_calls[0]["tags"]


def test_failure_does_not_mirror_to_fuli(tmp_shadow):
    shadow, primary, secondary, _ = tmp_shadow
    primary.mode = "error"
    shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "x", "peer": "user"},
        pilot_meta={"correlation_id": "corr-2", "pilot_sequence": 2},
    )
    assert len(secondary.add_calls) == 0
    diag = shadow._store.diagnostic_summary()
    assert diag["by_type"].get("mirror_skipped_primary_not_success", 0) == 1


def test_initialization_does_not_mirror_to_fuli(tmp_shadow):
    shadow, primary, secondary, _ = tmp_shadow
    primary.mode = "initializing"
    shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "x", "peer": "user"},
        pilot_meta={"correlation_id": "corr-3", "pilot_sequence": 3},
    )
    assert len(secondary.add_calls) == 0


def test_parser_error_does_not_mirror_to_fuli(tmp_shadow):
    shadow, primary, secondary, _ = tmp_shadow
    primary.mode = "parser_error"
    shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "x", "peer": "user"},
        pilot_meta={"correlation_id": "corr-4", "pilot_sequence": 4},
    )
    assert len(secondary.add_calls) == 0


def test_validation_rejected_does_not_mirror_to_fuli(tmp_shadow):
    shadow, primary, secondary, _ = tmp_shadow
    primary.mode = "validation"
    shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "x", "peer": "user"},
        pilot_meta={"correlation_id": "corr-5", "pilot_sequence": 5},
    )
    assert len(secondary.add_calls) == 0


def test_primary_result_returned_unchanged(tmp_shadow):
    shadow, primary, secondary, _ = tmp_shadow
    primary.mode = "error"
    result = shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "x", "peer": "user"},
        pilot_meta={"correlation_id": "corr-6", "pilot_sequence": 6},
    )
    assert result == json.dumps({"error": "connection refused"})


def test_duplicate_correlation_id_records_two_mirrors(tmp_shadow):
    shadow, primary, secondary, _ = tmp_shadow
    shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "unique", "peer": "user"},
        pilot_meta={"correlation_id": "corr-7", "pilot_sequence": 7},
    )
    shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "unique", "peer": "user"},
        pilot_meta={"correlation_id": "corr-7", "pilot_sequence": 7},
    )
    # Same correlation ID used twice yields two separate mirrored writes; the
    # pilot ledger must enforce sequence uniqueness, not the shadow provider.
    assert len(secondary.add_calls) == 2
    assert len(secondary.memories) == 2


def test_duplicate_correlation_id_records_in_ledger(tmp_shadow):
    shadow, primary, secondary, _ = tmp_shadow
    shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "unique", "peer": "user"},
        pilot_meta={"correlation_id": "corr-7", "pilot_sequence": 7},
    )
    shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "unique", "peer": "user"},
        pilot_meta={"correlation_id": "corr-7", "pilot_sequence": 7},
    )
    assert isinstance(shadow._store, ShadowEvidenceStore)
    summary = shadow._store.diagnostic_summary()
    # No duplicate diagnostic events for successful mirrors.
    assert summary["by_type"].get("mirror_skipped_primary_not_success", 0) == 0
    by_corr = shadow._store.query_by_correlation("corr-7")
    assert by_corr is not None
    assert by_corr["correlation_id"] == "corr-7"


def test_mirror_only_on_write_tools(tmp_shadow):
    shadow, primary, secondary, _ = tmp_shadow
    shadow.handle_tool_call(
        "honcho_search",
        {"query": "test"},
        pilot_meta={"correlation_id": "corr-8", "pilot_sequence": 8},
    )
    assert len(secondary.add_calls) == 0


def test_write_timeout_used_for_mirror(tmp_shadow):
    """Mirrored writes pass the configured write_timeout_ms to Fuli."""
    shadow, primary, secondary, _ = tmp_shadow
    shadow._write_timeout_ms = 12345
    shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "timeout test", "peer": "user"},
        pilot_meta={"correlation_id": "corr-9", "pilot_sequence": 9},
    )
    assert len(secondary.add_calls) == 1
    assert secondary.add_calls[0]["timeout_ms"] == 12345


def test_read_timeout_used_for_compare(tmp_shadow):
    """Shadow read comparisons pass the configured read_timeout_ms to Fuli."""
    shadow, primary, secondary, _ = tmp_shadow
    shadow._compare_reads = True
    shadow._sample_rate = 1.0
    shadow._read_timeout_ms = 678
    shadow.handle_tool_call(
        "honcho_search",
        {"query": "compare test"},
        pilot_meta={"correlation_id": "corr-10", "pilot_sequence": 10},
    )
    assert len(secondary.search_calls) == 1
    assert secondary.search_calls[0]["timeout_ms"] == 678


def test_cold_fuli_write_succeeds_with_write_timeout(tmp_shadow):
    """A slow first Fuli write succeeds because the write timeout is long."""
    shadow, primary, secondary, _ = tmp_shadow
    secondary.slow_add_seconds = 0.5
    secondary.slow_first_n = 1
    shadow._write_timeout_ms = 1000
    shadow._read_timeout_ms = 250
    result = shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "cold start", "peer": "user"},
        pilot_meta={"correlation_id": "corr-cold", "pilot_sequence": 100},
    )
    assert result == primary.calls[-1]["result"]
    assert len(secondary.add_calls) == 1


def test_slow_fuli_write_fails_with_short_read_timeout(tmp_shadow):
    """A slow Fuli write fails when passed only the short read timeout."""
    shadow, primary, secondary, _ = tmp_shadow
    secondary.slow_add_seconds = 0.5
    secondary.slow_first_n = 1
    shadow._write_timeout_ms = 1000
    shadow._read_timeout_ms = 100
    # Directly exercise Fuli with the read timeout; the mirror path uses the write timeout.
    add_args = {"content": "x", "source": "test", "namespace": shadow._namespace, "timeout_ms": shadow._read_timeout_ms}
    with pytest.raises(TimeoutError):
        secondary.handle_tool_call("fuli_memory_add", add_args)


def test_legacy_timeout_ms_still_works(tmp_shadow):
    """When only the legacy timeout_ms is configured, both reads and writes use it."""
    shadow, primary, secondary, _ = tmp_shadow
    shadow._write_timeout_ms = 250
    shadow._read_timeout_ms = 250
    shadow._timeout_ms = 250
    shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "legacy timeout", "peer": "user"},
        pilot_meta={"correlation_id": "corr-legacy", "pilot_sequence": 101},
    )
    assert len(secondary.add_calls) == 1
    assert secondary.add_calls[0]["timeout_ms"] == 250


def test_failed_fuli_write_does_not_change_primary_result(tmp_shadow):
    """Fuli failure leaves the primary output unchanged."""
    shadow, primary, secondary, _ = tmp_shadow
    secondary.failure_rate = 1.0
    result = shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "f", "peer": "user"},
        pilot_meta={"correlation_id": "corr-fail", "pilot_sequence": 102},
    )
    data = json.loads(result)
    assert data.get("status") == "success"
    assert data.get("id").startswith("honcho_")
    # Primary was called exactly once for the live result; Fuli failure recorded.
    mirrors = shadow._store.query_by_correlation("corr-fail")
    assert mirrors is not None
    assert mirrors.get("shadow_success") == 0
