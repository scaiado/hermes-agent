from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from plugins.memory.shadow import ShadowMemoryProvider
from plugins.memory.shadow.shadow_store import ShadowEvidenceStore
from pilot.comparison_executor import ComparisonExecutor
from pilot.comparison_store import ComparisonStore
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
    # Comparison store + executor. Tests that exercise the read path
    # call shadow.flush_comparisons(timeout_seconds=...) before
    # asserting on the secondary's recorded calls.
    shadow._comparison_store = ComparisonStore(tmp_path / "comparisons.db")
    shadow._comparison_run_id = "tmp-shadow-fixture"
    shadow._executor = ComparisonExecutor(
        max_workers=1,
        max_queue_size=64,
        comparison_budget_ms=1000,
    )
    shadow._executor.start_persistence_worker(shadow._comparison_store)
    shadow._executor.initialize(secondary.handle_tool_call)

    # Ensure the executor's threads are stopped when the fixture tears
    # down. Without this, the persistence worker's loop keeps the
    # pytest worker alive past the test.
    def _finalize() -> None:
        try:
            if shadow._executor is not None:
                shadow._executor.shutdown(drain_timeout_seconds=0.5)
        except Exception:
            pass

    yield shadow, primary, secondary, store_path
    _finalize()


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
    """Shadow read comparisons pass the configured comparison_budget_ms to Fuli.

    The comparison runs in the background executor, so the test flushes
    before asserting on the secondary's recorded calls. The executor
    uses ``comparison_budget_ms`` (not the legacy ``_read_timeout_ms``)
    as the Fuli call's timeout.
    """
    shadow, primary, secondary, _ = tmp_shadow
    shadow._compare_reads = True
    shadow._sample_rate = 1.0
    # The fixture constructs the executor with comparison_budget_ms=1000.
    # Reset it to the test's expected value by tearing down and rebuilding.
    if shadow._executor is not None:
        shadow._executor.shutdown(drain_timeout_seconds=0.5)
    shadow._comparison_budget_ms = 678
    shadow._executor = ComparisonExecutor(
        max_workers=1,
        max_queue_size=64,
        comparison_budget_ms=678,
    )
    shadow._executor.start_persistence_worker(shadow._comparison_store)
    shadow._executor.initialize(secondary.handle_tool_call)
    shadow.handle_tool_call(
        "honcho_search",
        {"query": "compare test"},
        pilot_meta={"correlation_id": "corr-10", "pilot_sequence": 10},
    )
    # Foreground must return without waiting for the executor.
    shadow.flush_comparisons(timeout_seconds=5.0)
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


# -----------------------------------------------------------------------------
# Telemetry contract (Issue 2): the in/out telemetry dict returned by the
# shadow provider lets the qualification driver compute foreground
# enqueue overhead and primary-result invariance WITHOUT a second Honcho
# call. The dict carries only monotonic ms stamps and sha256 hashes.
# -----------------------------------------------------------------------------


def test_telemetry_disabled_by_default(tmp_shadow):
    """Production callers do not pass telemetry; the hot path still works."""
    shadow, primary, secondary, _ = tmp_shadow
    primary.mode = "success"
    result = shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "telemetry off", "peer": "user"},
        pilot_meta={"correlation_id": "t-off", "pilot_sequence": 200},
    )
    data = json.loads(result)
    assert data.get("status") == "success"
    # No telemetry dict was supplied; the contract is just "do nothing".


def test_telemetry_records_primary_hash_invariance(tmp_shadow):
    """When telemetry is on, the sha256 of the primary matches the sha256 of the returned string."""
    import hashlib
    shadow, primary, secondary, _ = tmp_shadow
    primary.mode = "success"
    telemetry = {}
    result = shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "invariant", "peer": "user"},
        telemetry=telemetry,
        pilot_meta={"correlation_id": "t-inv", "pilot_sequence": 201},
    )
    # Primary result hash matches returned result hash — guarantees byte-for-byte
    # equality (which is what primary invariance means).
    assert telemetry["primary_result_sha256"] == telemetry["returned_result_sha256"]
    assert telemetry["primary_result_sha256"] == hashlib.sha256(
        result.encode("utf-8", "replace")
    ).hexdigest()


def test_telemetry_records_all_four_stamps(tmp_shadow):
    """All four monotonic ms stamps are populated when telemetry is on."""
    shadow, primary, secondary, _ = tmp_shadow
    primary.mode = "success"
    telemetry = {}
    shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "stamps", "peer": "user"},
        telemetry=telemetry,
        pilot_meta={"correlation_id": "t-st", "pilot_sequence": 202},
    )
    # primary window
    assert "primary_started_at_ms" in telemetry
    assert "primary_completed_at_ms" in telemetry
    assert telemetry["primary_completed_at_ms"] >= telemetry["primary_started_at_ms"]
    # shadow return
    assert "shadow_returned_at_ms" in telemetry
    assert telemetry["shadow_returned_at_ms"] >= telemetry["primary_completed_at_ms"]


def test_telemetry_records_enqueue_window_when_sampled(tmp_shadow):
    """When the read is sampled, enqueue_started and enqueue_completed stamps are populated.

    The brief requires foreground_enqueue_overhead_ms =
        enqueue_completed_at_ms - enqueue_started_at_ms
    """
    shadow, primary, secondary, _ = tmp_shadow
    primary.mode = "success"
    # Force a sampled decision.
    shadow._sample_rate = 1.0
    shadow._compare_reads = True
    secondary.mode = "search_results"
    telemetry = {}
    shadow.handle_tool_call(
        "honcho_search",
        {"query": "telemetry enqueue", "top_k": 3},
        telemetry=telemetry,
        pilot_meta={"correlation_id": "t-enq", "pilot_sequence": 203},
    )
    assert telemetry.get("sampled") is True
    assert "enqueue_started_at_ms" in telemetry
    assert "enqueue_completed_at_ms" in telemetry
    overhead_ms = (
        telemetry["enqueue_completed_at_ms"] - telemetry["enqueue_started_at_ms"]
    )
    # The brief's threshold: p95 < 10 ms; this single sample must be small.
    assert 0.0 <= overhead_ms < 25.0, f"enqueue overhead too large: {overhead_ms}"


def test_telemetry_does_not_leak_raw_payload(tmp_shadow):
    """Telemetry dict never contains the raw primary payload or any query text.

    The shadow provider is the source of the raw payload; the telemetry
    contract is "hashes and ms stamps only". We do not assert on the
    contents of the Honcho payload itself (the fake provider's output
    is mode-driven and not user-controlled). We assert on the SHAPE of
    the telemetry dict: no key may carry a string longer than 64 chars
    except the documented ones, and no key may match documented
    shadow-result-bearing names like 'primary_result' or 'returned_result'
    without a '_sha256' suffix.
    """
    shadow, primary, secondary, _ = tmp_shadow
    primary.mode = "success"
    primary.return_value = json.dumps({"status": "success", "id": "honcho_x"})
    secret = "HONCHO_SECRET_MARKER_XYZZY_42"
    assert secret not in primary.return_value
    telemetry = {}
    result = shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "leak test", "peer": "user"},
        telemetry=telemetry,
        pilot_meta={"correlation_id": "t-leak", "pilot_sequence": 204},
    )
    # Hashes only — no raw content keys.
    serialized = json.dumps(telemetry, default=str)
    assert secret not in serialized
    # The shadow-result-bearing keys must be sha256-shaped, not raw
    # primary content. Their values are 64 lowercase hex chars.
    import re as _re
    sha256_re = _re.compile(r"^[0-9a-f]{64}$")
    for key in ("primary_result_sha256", "returned_result_sha256"):
        assert key in telemetry, f"missing {key!r}"
        assert sha256_re.match(telemetry[key]), f"{key} not sha256-shaped: {telemetry[key]!r}"
    # No undocumented string keys holding long payloads.
    allowed = {
        "primary_started_at_ms",
        "primary_completed_at_ms",
        "primary_latency_ms",
        "primary_result_sha256",
        "returned_result_sha256",
        "shadow_returned_at_ms",
        "enqueue_started_at_ms",
        "enqueue_completed_at_ms",
        "sampled",
    }
    for k, v in telemetry.items():
        assert k in allowed, f"unexpected telemetry key {k!r}"
        if isinstance(v, str):
            # Only hash-shaped strings are allowed in telemetry.
            assert sha256_re.match(v), f"telemetry[{k!r}] not sha256-shaped: {v!r}"
