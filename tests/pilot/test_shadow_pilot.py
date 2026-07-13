from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Set

import pytest

from pilot.ledger import PilotLedger
from plugins.memory.shadow import ShadowMemoryProvider
from tests.plugins.fake_providers import FakeFuliProvider, FakeHonchoProvider


def _run_pilot_loop(
    shadow: ShadowMemoryProvider,
    ledger: PilotLedger,
    n: int,
    failure_indices: Set[int] = None,
    fuli_failure_indices: Set[int] = None,
) -> Dict[str, Any]:
    from pilot.primary_classifier import classify_honcho_result

    failure_indices = set(failure_indices or set())
    fuli_failure_indices = set(fuli_failure_indices or set())
    primary = shadow._primary
    assert isinstance(primary, FakeHonchoProvider)
    secondary = shadow._secondary
    assert isinstance(secondary, FakeFuliProvider)
    for i in range(n):
        if i in failure_indices:
            primary.mode = "error"
        elif i in fuli_failure_indices:
            primary.mode = "success"
            secondary.failure_rate = 1.0
        else:
            primary.mode = "success"
            secondary.failure_rate = 0.0

        corr = ledger.start_operation(i, "conclude", f"fp-{i}")
        pilot_meta = {
            "correlation_id": corr,
            "pilot_run_id": "det-run",
            "pilot_sequence": i,
            "synthetic": True,
        }
        start = time.monotonic()
        primary_raw = primary.handle_tool_call("honcho_conclude", {"conclusion": f"fact {i}"}, pilot_meta=pilot_meta)
        latency_ms = (time.monotonic() - start) * 1000.0
        primary_class = classify_honcho_result(primary_raw, latency_ms=latency_ms)
        ledger.record_primary_result(corr, primary_class)

        if primary_class.is_confirmed_success:
            shadow.handle_tool_call(
                "honcho_conclude",
                {"conclusion": f"fact {i}"},
                pilot_meta=pilot_meta,
            )
            outcome = shadow._store.query_by_correlation(corr)
            assert outcome is not None
            ledger.record_mirror_result(
                corr,
                memory_id=outcome.get("memory_id"),
                accepted=bool(outcome.get("shadow_success")),
                indexed=bool(outcome.get("shadow_success")),
                pending=False,
                failed=not bool(outcome.get("shadow_success")),
                latency_ms=outcome.get("latency_ms"),
                error_code=("fuli_failed" if not outcome.get("shadow_success") else None),
                error_message=outcome.get("shadow_error"),
            )
        # else: no mirror, ledger already has primary_failed

    # Simulate drain: all accepted memories become indexed.
    rows = ledger.get_all_rows()
    for r in rows:
        if r.fuli_accepted and not r.fuli_indexed and not r.fuli_failed:
            ledger.update_indexing_status(r.correlation_id, indexed=True, pending=False, failed=False)
    return ledger.compute_report()


def test_deterministic_pilot_accounting(tmp_path):
    store_path = tmp_path / "shadow.db"
    ledger_path = tmp_path / "ledger.db"
    primary = FakeHonchoProvider(mode="success")
    secondary = FakeFuliProvider()
    shadow = ShadowMemoryProvider()
    shadow._primary = primary
    shadow._secondary = secondary
    shadow._enabled = True
    shadow._mirror_writes = True
    shadow._compare_reads = False
    shadow._sample_rate = 0.0
    shadow._namespace = "hermes:shadow-pilot"
    shadow._capture_content = True
    shadow._store = shadow._create_store(str(store_path))

    ledger = PilotLedger(ledger_path, run_id="det-run")
    report = _run_pilot_loop(
        shadow,
        ledger,
        n=100,
        failure_indices={5, 10, 15},
        fuli_failure_indices={20, 21, 22},
    )

    assert report["total_attempts"] == 100
    assert report["primary_success"] == 97
    assert report["primary_failure"] == 3
    assert report["mirror_attempted"] == 97
    assert report["indexed"] == 94
    assert report["failed"] == 3
    assert report["pending"] == 0
    assert report["unexplained_mirror_attempts"] == 0
    assert report["balanced"] is True

    # Verify mirror only on success.
    assert len(secondary.add_calls) == 97

    # Verify primary result unchanged.
    primary.mode = "error"
    raw = shadow.handle_tool_call("honcho_conclude", {"conclusion": "x"}, pilot_meta={"correlation_id": "z"})
    assert raw == json.dumps({"error": "connection refused"})


def test_correlation_propagation(tmp_path):
    store_path = tmp_path / "shadow.db"
    primary = FakeHonchoProvider(mode="success")
    secondary = FakeFuliProvider()
    shadow = ShadowMemoryProvider()
    shadow._primary = primary
    shadow._secondary = secondary
    shadow._enabled = True
    shadow._mirror_writes = True
    shadow._namespace = "hermes:shadow-pilot"
    shadow._store = shadow._create_store(str(store_path))

    corr = "corr-test-001"
    shadow.handle_tool_call(
        "honcho_conclude",
        {"conclusion": "test"},
        pilot_meta={"correlation_id": corr, "pilot_run_id": "corr-run", "pilot_sequence": 0},
    )
    outcome = shadow._store.query_by_correlation(corr)
    assert outcome is not None
    assert outcome["correlation_id"] == corr


def test_pilot_cli_routes(tmp_path):
    from hermes_cli.subcommands.shadow import build_shadow_parser
    from hermes_cli.shadow_cmd import cmd_shadow

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    build_shadow_parser(subparsers, cmd_shadow=cmd_shadow)

    for sub in ["preflight", "status", "report"]:
        args = parser.parse_args(["shadow", sub])
        assert getattr(args, "shadow_command") == sub
        assert args.func is cmd_shadow

    # Unknown subcommand should be rejected by argparse, not fall through.
    with pytest.raises(SystemExit):
        parser.parse_args(["shadow", "nonexistent"])


def test_pilot_cli_threshold_flags_are_wired_into_config():
    """The new --primary-success-rate-required and --minimum-primary-success-required
    flags must end up in the PilotConfig and propagate to the report. Without
    this test, future refactors could break reproducibility silently.
    """
    from pilot.shadow_pilot import (
        PilotConfig,
        _config_from_args,
        build_pilot_argparser,
    )

    parser = build_pilot_argparser()
    args = parser.parse_args([
        "--primary-success-rate-required", "99.0",
        "--minimum-primary-success-required", "50",
    ])
    config = _config_from_args(args)
    assert config.primary_success_rate_required_percent == 99.0
    assert config.minimum_primary_success_required == 50

    # And the 4-hour default remains in place when the flag is omitted.
    config_default = PilotConfig()
    assert config_default.primary_success_rate_required_percent == 99.9
    assert config_default.minimum_primary_success_required == 200
