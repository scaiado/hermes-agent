"""End-to-end Phase 5 integration test."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import patch

from fuli_product import FuliProductLifecycle
from fuli_product.adapters import InMemoryConfigRepository
from fuli_product.reporting import Reporter

from ._fakes import FakeComparisonRuntime


COMPAT_OK = {
    "hermes_version": "0.19.0",
    "fuli_info": {"available": True, "version": "0.0.0", "commit": "727ce92603707619e0155a6c0ca1a01f5f2e07c4"},
    "incompatible": False,
    "reasons": [],
}


def _config_repo(mode: str = "off") -> InMemoryConfigRepository:
    return InMemoryConfigRepository({"memory": {"fuli_product": {"mode": mode, "sample_rate": 1.0, "compare_reads": True}}})


def _compat_patch():
    return patch("fuli_product.lifecycle.check_compatibility", return_value=COMPAT_OK)


def test_lifecycle_runtime_reporter_agree_on_state():
    repo = _config_repo("off")
    lifecycle = FuliProductLifecycle(repo)
    runtime = FakeComparisonRuntime()
    reporter = Reporter(repo, hermes_home=Path("/tmp/fuli_test"), runtime=runtime)

    # 1. Initial mode off.
    assert lifecycle.cfg.mode == "off"

    # 2-3. Enable shadow + initialize runtime (with compat patched to compatible).
    with _compat_patch():
        cfg, _ = lifecycle.enable_shadow(
            primary="honcho", secondary="fuli", sample_rate=1.0, dry_run=False, fuli_available=True
        )
    assert cfg.mode == "shadow"
    runtime.initialize()
    lifecycle.register_runtime(runtime)

    # 4. Status reports shadow + healthy.
    runtime.accounting_value = {"sampled": 10, "completed": 10, "started": 10, "enqueued": 10, "persisted": 10}
    with patch("fuli_product.reporting.check_compatibility", return_value=COMPAT_OK), \
         patch("fuli_product.reporting.get_fuli_version", return_value=COMPAT_OK["fuli_info"]):
        status = reporter.status()
    assert status["mode"] == "shadow"
    assert status["state"] in {"healthy", "degraded"}

    # 5. Inject primary_mutation hard gate.
    runtime.accounting_value = {"primary_mutation": 1}

    # 6. Pause triggers runtime shutdown + persists mode.
    cfg, _ = lifecycle.pause(reason="auto-rollback-test")
    assert cfg.mode == "paused"

    # 7. previous_mode preserved.
    assert cfg.previous_mode == "shadow"
    assert runtime._shutdown_calls >= 1

    # 8-9. Report after fault cleared shows paused state.
    runtime.accounting_value = {}
    with patch("fuli_product.reporting.check_compatibility", return_value=COMPAT_OK), \
         patch("fuli_product.reporting.get_fuli_version", return_value=COMPAT_OK["fuli_info"]):
        status = reporter.status()
    assert any("paused" in r.lower() or "Operator paused" in r for r in status["health"]["reasons"])

    # 10-11. Re-enable.
    runtime.accounting_value = {"sampled": 5, "completed": 5, "started": 5, "enqueued": 5, "persisted": 5}
    with _compat_patch():
        cfg, _ = lifecycle.enable_shadow(
            primary="honcho", secondary="fuli", sample_rate=1.0, dry_run=False, fuli_available=True
        )
    assert cfg.mode == "shadow"

    # 12-13. Disable cleanly.
    cfg, _ = lifecycle.disable(reason="integration-test-done")
    assert cfg.mode == "off"
    assert cfg.previous_mode == "shadow"
    runtime.shutdown(drain_timeout_seconds=2.0)

    # 14. Final state off.
    with patch("fuli_product.reporting.check_compatibility", return_value=COMPAT_OK), \
         patch("fuli_product.reporting.get_fuli_version", return_value=COMPAT_OK["fuli_info"]):
        final_status = reporter.status()
    assert final_status["mode"] == "off"


def test_pause_failure_path_does_not_corrupt_config():
    repo = _config_repo("shadow")
    lifecycle = FuliProductLifecycle(repo)
    runtime = FakeComparisonRuntime()
    lifecycle.register_runtime(runtime)
    runtime.initialize()
    cfg, _ = lifecycle.pause(reason="test")
    assert cfg.mode == "paused"
    assert cfg.previous_mode == "shadow"
    assert runtime._shutdown_calls >= 1


def test_reporter_safe_when_runtime_missing():
    repo = _config_repo("shadow")
    reporter = Reporter(repo, hermes_home=Path("/tmp/fuli_test"), runtime=None)
    status = reporter.status()
    assert status["executor_accounting"]["primary_mutation"] == 0
    assert status["state"] in {"healthy", "degraded", "auto_paused", "unavailable", "incompatible"}


def test_export_round_trip_is_valid_json(tmp_path: Path):
    repo = _config_repo("shadow")
    runtime = FakeComparisonRuntime(accounting_value={"completed": 7})
    reporter = Reporter(repo, hermes_home=Path("/tmp/fuli_test"), runtime=runtime)
    out = reporter.export(redacted=True, output=tmp_path / "export.json")
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["status"]["mode"] == "shadow"


def test_no_residual_threads_after_disable():
    initial = {t.ident for t in threading.enumerate()}
    repo = _config_repo("shadow")
    lifecycle = FuliProductLifecycle(repo)
    runtime = FakeComparisonRuntime()
    lifecycle.register_runtime(runtime)
    runtime.initialize()
    lifecycle.disable(reason="thread-leak-check")
    runtime.shutdown(drain_timeout_seconds=2.0)
    final = {t.ident for t in threading.enumerate()}
    assert final.issubset(initial)
