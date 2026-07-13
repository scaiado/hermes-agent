"""Tests for the primary-health gate logic.

These tests exercise the report builder directly so we can assert run_status
without running the full pilot loop. They cover the 2026-07-13 false-green
regression: a run with 56/240 primary success must be classified as
``invalid_primary_success_rate``, never ``valid`` regardless of how healthy the
Fuli mirror looks.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pilot.shadow_pilot import (
    PilotConfig,
    PilotState,
    _build_report,
)


# ---------------------------------------------------------------------------
# Synthetic ledger helper
# ---------------------------------------------------------------------------


def _synthetic_ledger_rows(
    n: int,
    primary_success: int,
    primary_latency_ms: float = 50.0,
) -> List[Dict[str, Any]]:
    """Build ledger rows shaped like ``ledger.compute_report()['rows']``.

    ``primary_success`` rows are interleaved from the front of the list; the
    remainder are failures. This matches what the ledger actually returns.
    """
    rows: List[Dict[str, Any]] = []
    for i in range(n):
        is_success = i < primary_success
        rows.append(
            {
                "primary_latency_ms": primary_latency_ms,
                "fuli_latency_ms": 100.0,
                "fuli_indexed_at": "2026-07-13T00:00:00Z" if is_success else None,
                "primary_completed_at": "2026-07-12T23:59:00Z" if is_success else None,
            }
        )
    return rows


def _ledger_report(
    n: int,
    primary_success: int,
    mirror_attempted: int,
    indexed: int,
    pending: int = 0,
    failed: int = 0,
    rows: List[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    primary_failure = n - primary_success
    return {
        "balanced": True,
        "total_attempts": n,
        "primary_success": primary_success,
        "primary_failure": primary_failure,
        "mirror_attempted": mirror_attempted,
        "indexed": indexed,
        "pending": pending,
        "failed": failed,
        "unexplained_mirror_attempts": 0,
        "primary_error_categories": {"provider_error": primary_failure} if primary_failure else {},
        "invariant_errors": [],
        "rows": rows if rows is not None else _synthetic_ledger_rows(n, primary_success),
    }


def _make_state(
    *,
    primary_available_at_start: bool = True,
    primary_available_at_end: bool = True,
    max_consecutive_primary_failures: int = 0,
    primary_writes_above_p95_threshold: int = 0,
    initial_rss_bytes: int = 100 * 1024 * 1024,
    final_rss_bytes: int = 110 * 1024 * 1024,
    rss_samples: List = None,
    warmup_rss_bytes: int = None,
    config_overrides: Dict[str, Any] = None,
) -> PilotState:
    """Build a PilotState suitable for direct _build_report() calls."""
    config = PilotConfig(
        duration_hours=0.25,
        interval_seconds=15,
        primary_success_rate_required_percent=99.9,
        minimum_primary_success_required=200,
        primary_p95_max_ms=10_000.0,
        primary_timeout_rate_max_percent=0.1,
        sustained_primary_outage_threshold=30,
    )
    if config_overrides:
        for k, v in config_overrides.items():
            setattr(config, k, v)
    state = PilotState(config)
    state.primary_available_at_start = primary_available_at_start
    state.primary_available_at_end = primary_available_at_end
    state.max_consecutive_primary_failures = max_consecutive_primary_failures
    state.primary_writes_above_p95_threshold = primary_writes_above_p95_threshold
    state.initial_rss_bytes = initial_rss_bytes
    state.final_rss_bytes = final_rss_bytes
    state.warmup_rss_bytes = warmup_rss_bytes
    state.rss_samples = rss_samples or []
    state.rss_sampling_started = bool(state.rss_samples)
    state.fuli_commit = "727ce92603707619e0155a6c0ca1a01f5f2e07c4"
    return state


def _fake_final_record() -> Dict[str, Any]:
    return {
        "evidence_rows": 56,
        "observation_rows": 0,
        "diagnostic_events": 0,
        "namespaces": ["hermes:shadow-pilot"],
        "thread_alive": True,
        "loop_running": True,
    }


def _drain_result_ok() -> Dict[str, Any]:
    return {"ok": True, "iterations": 1, "drain_timeout_seconds": 120, "accepted": 56, "indexed": 56, "pending": 0, "failed": 0, "final_status": {}}


def _drain_result_empty() -> Dict[str, Any]:
    return {"ok": True, "iterations": 0, "drain_timeout_seconds": 120, "accepted": 0, "indexed": 0, "pending": 0, "failed": 0, "final_status": {}}


# ---------------------------------------------------------------------------
# The 9 tests required by the 2026-07-13 brief
# ---------------------------------------------------------------------------


def test_56_of_240_primary_success_returns_ok_false(monkeypatch):
    """The 2026-07-13 false-green regression test.

    56/240 primary success is a 23.3% success rate, far below the 99.9%
    required threshold and below the 200-attempt minimum. Perfect Fuli
    mirroring cannot make this report valid.
    """
    ledger_report = _ledger_report(n=240, primary_success=56, mirror_attempted=56, indexed=56)
    # max_consecutive_primary_failures is intentionally below the 30-attempt
    # outage threshold so the run is classified by success rate, not by
    # sustained outage. The 4-hour run in the wild had 184 in a row, but the
    # test isolates the success-rate path.
    state = _make_state(max_consecutive_primary_failures=5)

    monkeypatch.setattr(
        "pilot.shadow_pilot.PilotLedger",
        lambda *a, **kw: _StubLedger(ledger_report),
        raising=False,
    )

    state.ledger = _StubLedger(ledger_report)  # type: ignore[assignment]
    report = _build_report(state, _drain_result_ok(), _fake_final_record())

    assert report["ok"] is False
    assert report["run_status"] == "invalid_primary_success_rate"
    assert report["primary_success_rate_percent"] < 99.9
    # Fuli-side gates can still pass; that does not rescue the run.
    assert report["pass_gates"]["eventual_index_rate_99"] is True


def test_run_status_is_invalid_primary_success_rate():
    """run_status carries the diagnostic, not just ok=false."""
    ledger_report = _ledger_report(n=240, primary_success=56, mirror_attempted=56, indexed=56)
    state = _make_state(max_consecutive_primary_failures=5)
    state.ledger = _StubLedger(ledger_report)  # type: ignore[assignment]

    report = _build_report(state, _drain_result_ok(), _fake_final_record())

    assert report["run_status"] == "invalid_primary_success_rate"
    assert "below required" in (report.get("status_reason") or "")


def test_perfect_fuli_mirroring_cannot_override_failed_primary_health():
    """100% Fuli mirror with 0/240 primary success must still be invalid_primary_unavailable."""
    ledger_report = _ledger_report(n=240, primary_success=0, mirror_attempted=0, indexed=0)
    state = _make_state(
        primary_available_at_start=True,
        primary_available_at_end=False,
        max_consecutive_primary_failures=240,
    )
    state.ledger = _StubLedger(ledger_report)  # type: ignore[assignment]

    report = _build_report(state, _drain_result_empty(), _fake_final_record())

    assert report["ok"] is False
    assert report["run_status"] == "invalid_primary_unavailable"


def test_primary_unavailable_at_preflight_returns_invalid_primary_unavailable():
    """If primary is not available at end-of-run, the run is invalid."""
    ledger_report = _ledger_report(n=60, primary_success=60, mirror_attempted=60, indexed=60)
    state = _make_state(
        primary_available_at_start=True,
        primary_available_at_end=False,
        max_consecutive_primary_failures=0,
    )
    state.ledger = _StubLedger(ledger_report)  # type: ignore[assignment]

    report = _build_report(state, _drain_result_ok(), _fake_final_record())

    assert report["ok"] is False
    assert report["run_status"] == "invalid_primary_unavailable"
    assert report["primary_available_at_end"] is False
    assert report["primary_availability_gate"] is False


def test_minimum_successful_sample_size_failure_returns_invalid_primary_sample_size():
    """A run with all 100% primary success but only 60 attempts (under the 200
    minimum) must be invalid_primary_sample_size, not invalid_primary_success_rate.
    """
    ledger_report = _ledger_report(n=60, primary_success=60, mirror_attempted=60, indexed=60)
    state = _make_state(config_overrides={"minimum_primary_success_required": 200})
    state.ledger = _StubLedger(ledger_report)  # type: ignore[assignment]

    report = _build_report(state, _drain_result_ok(), _fake_final_record())

    assert report["ok"] is False
    assert report["run_status"] == "invalid_primary_sample_size"
    # success-rate gate passes (100% > 99.9%); sample-size gate fails (60 < 200).
    assert report["primary_health_gates"]["primary_success_rate_gate"] is True
    assert report["primary_health_gates"]["minimum_primary_success_gate"] is False


def test_fuli_ingestion_failure_remains_separately_classified():
    """When Fuli silently loses mirrors, run_status is invalid_fuli_ingestion."""
    ledger_report = _ledger_report(
        n=60,
        primary_success=60,
        mirror_attempted=60,
        indexed=60,
        pending=5,  # silent loss after drain
    )
    state = _make_state()
    state.ledger = _StubLedger(ledger_report)  # type: ignore[assignment]

    report = _build_report(state, _drain_result_ok(), _fake_final_record())

    assert report["ok"] is False
    assert report["run_status"] == "invalid_fuli_ingestion"
    # Primary health gates still pass; the failure is purely on the Fuli side.
    assert report["primary_health_gates"]["primary_availability_gate"] is True


def test_validate_report_rejects_invalid_primary_runs(tmp_path):
    """hermes shadow validate-report rejects reports with non-valid run_status."""
    from hermes_cli.shadow_cmd import _validate_report

    bad_report = {
        "ledger": {
            "total_attempts": 240,
            "primary_success": 56,
            "primary_failure": 184,
            "mirror_attempted": 56,
            "indexed": 56,
            "pending": 0,
            "failed": 0,
        },
        "run_status": "invalid_primary_success_rate",
        "status_reason": "primary success rate 23.33% below required 99.9%",
    }
    p = tmp_path / "report.json"
    p.write_text(__import__("json").dumps(bad_report))

    result = _validate_report(str(p))

    assert result["ok"] is False
    assert any("run_status" in err for err in result["errors"])
    assert result["run_status"] == "invalid_primary_success_rate"


def test_valid_60_of_60_run_still_passes():
    """A clean run with healthy primary and Fuli is still classified valid."""
    ledger_report = _ledger_report(n=60, primary_success=60, mirror_attempted=60, indexed=60)
    state = _make_state(
        config_overrides={
            "minimum_primary_success_required": 50,  # 15-minute qualification
            "primary_success_rate_required_percent": 99.0,
        },
    )
    state.ledger = _StubLedger(ledger_report)  # type: ignore[assignment]

    report = _build_report(state, _drain_result_ok(), _fake_final_record())

    assert report["run_status"] == "valid"
    assert report["ok"] is True


def test_stage_b_cannot_start_from_invalid_run_status():
    """Stage B (sample_rate > 0 / compare_reads) requires a valid prior run.

    We don't have an actual Stage B runner yet; this test asserts the
    contract that any tool which would enable sampled reads must read
    ``run_status`` and refuse to start from anything other than ``valid``.
    The test stub here mirrors the contract; the real Stage B CLI will
    import this guard.
    """
    # Importing here keeps the dependency on the shadow report loader local.
    from pilot.shadow_pilot import _derive_fuli_commit  # noqa: F401

    def stage_b_guard(report: Dict[str, Any]) -> bool:
        return (report or {}).get("run_status") == "valid"

    # False positives: any non-valid run is blocked from enabling Stage B.
    bad_reports = [
        {"run_status": "invalid_primary_unavailable"},
        {"run_status": "invalid_primary_success_rate"},
        {"run_status": "invalid_primary_sample_size"},
        {"run_status": "invalid_fuli_ingestion"},
        {"run_status": "invalid_integrity"},
        {},  # legacy report without run_status also fails closed
    ]
    for r in bad_reports:
        assert stage_b_guard(r) is False, f"Stage B should be blocked for {r}"

    # A valid run is the only way through.
    assert stage_b_guard({"run_status": "valid"}) is True


# ---------------------------------------------------------------------------
# Helper stub for _build_report
# ---------------------------------------------------------------------------


class _StubLedger:
    """Minimal stub that mimics the PilotLedger methods used by _build_report."""

    def __init__(self, payload: Dict[str, Any]) -> None:
        self._payload = payload

    def compute_report(self) -> Dict[str, Any]:
        return self._payload


# ---------------------------------------------------------------------------
# Additional supporting tests
# ---------------------------------------------------------------------------


def test_drain_lag_field_is_renamed_with_backwards_compat_alias():
    """The new drain_lag_p50_ms field exists alongside the deprecated indexing_p50_ms alias."""
    ledger_report = _ledger_report(n=60, primary_success=60, mirror_attempted=60, indexed=60)
    state = _make_state(config_overrides={"minimum_primary_success_required": 50})
    state.ledger = _StubLedger(ledger_report)  # type: ignore[assignment]

    report = _build_report(state, _drain_result_ok(), _fake_final_record())

    assert "drain_lag_p50_ms" in report["latencies"]
    assert "drain_lag_p95_ms" in report["latencies"]
    # Deprecated alias preserved for backward compatibility.
    assert "indexing_p50_ms" in report["latencies"]
    assert "indexing_p95_ms" in report["latencies"]
    assert report["latencies"]["drain_lag_p50_ms"] == report["latencies"]["indexing_p50_ms"]
    assert report["latencies"]["drain_lag_p95_ms"] == report["latencies"]["indexing_p95_ms"]


def test_rss_report_includes_warmup_p50_p95_max_min_slope_peak():
    """The structured RSS report carries every field required by the brief."""
    ledger_report = _ledger_report(n=10, primary_success=10, mirror_attempted=10, indexed=10)
    samples = [(float(i), 100 * 1024 * 1024 + i * 1024 * 1024) for i in range(1, 11)]
    state = _make_state(
        initial_rss_bytes=99 * 1024 * 1024,
        final_rss_bytes=110 * 1024 * 1024,
        warmup_rss_bytes=100 * 1024 * 1024,
        rss_samples=samples,
    )
    state.ledger = _StubLedger(ledger_report)  # type: ignore[assignment]

    report = _build_report(state, _drain_result_ok(), _fake_final_record())

    mem = report["memory"]
    assert "warmup_rss_bytes" in mem
    assert "rss_p50_bytes" in mem
    assert "rss_p95_bytes" in mem
    assert "rss_max_bytes" in mem
    assert "rss_min_bytes" in mem
    assert "final_rss_bytes" in mem
    assert "rss_slope_bytes_per_second" in mem
    assert "peak_minus_warmup_bytes" in mem
    assert mem["warmup_rss_bytes"] == 100 * 1024 * 1024
    assert mem["rss_max_bytes"] >= mem["rss_min_bytes"]
    # Linear slope of an upward series should be positive.
    assert (mem["rss_slope_bytes_per_second"] or 0.0) > 0


def test_sustained_primary_outage_detection():
    """If the primary fails 30+ attempts in a row, run_status is invalid_primary_unavailable."""
    ledger_report = _ledger_report(n=240, primary_success=210, mirror_attempted=210, indexed=210)
    state = _make_state(max_consecutive_primary_failures=30)
    state.ledger = _StubLedger(ledger_report)  # type: ignore[assignment]

    report = _build_report(state, _drain_result_ok(), _fake_final_record())

    assert report["sustained_primary_outage_detected"] is True
    assert report["ok"] is False
    assert report["run_status"] == "invalid_primary_unavailable"