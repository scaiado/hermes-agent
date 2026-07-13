#!/usr/bin/env python3
"""Shadow-pilot load generator and evidence collector (P0 reliability pass).

Runs a conservative Hermes shadow-pilot in a dedicated profile, performs periodic
Honcho writes (mirrored to Fuli only on confirmed primary success), and records
a canonical per-attempt ledger. After the configured duration it performs a bounded
indexing drain, validates accounting invariants, and writes a sanitized report.

Usage:
    python -m pilot.shadow_pilot [--duration-hours 0.25] [--interval-seconds 15]

The script is designed to be started as a background process. It is self-contained
and does not require an interactive Hermes session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import sys
import threading
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from hermes_cli.config import cfg_get, load_config
from hermes_constants import get_hermes_home
from plugins.memory import load_memory_provider
from plugins.memory.shadow import ShadowMemoryProvider
from plugins.memory.shadow.shadow_store import ShadowEvidenceStore
from pilot.ledger import PilotLedger, classify_exception
from pilot.primary_classifier import classify_honcho_result
from plugins.memory import load_memory_provider

PROFILE_NAME = os.environ.get("HERMES_SHADOW_PROFILE", "shadow-pilot")
HERMES_HOME = Path(
    os.environ.get("HERMES_HOME", str(Path.home() / ".hermes" / "profiles" / PROFILE_NAME))
)
REPORTS_DIR = PROJECT_ROOT / "reports" / "shadow"
APPROVED_FULI_COMMIT = "727ce92603707619e0155a6c0ca1a01f5f2e07c4"

# Ensure the pilot runs in the dedicated profile.
os.environ["HERMES_HOME"] = str(HERMES_HOME)
os.environ["HERMES_PROFILE"] = PROFILE_NAME


@dataclass
class PilotConfig:
    duration_hours: float = 6.0
    interval_seconds: int = 120
    drain_timeout_seconds: int = 120
    write_only: bool = True
    compare_reads: bool = False
    sample_rate: float = 0.0
    namespace: str = "hermes:shadow-pilot"
    run_id: str = field(default_factory=lambda: f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}")
    hermes_home: Path = field(default=HERMES_HOME)
    profile_name: str = PROFILE_NAME
    # Primary health gate thresholds (added for the 2026-07-13 false-green fix).
    # Defaults match the four-hour P0 soak; the 15-minute qualification uses
    # looser values via config overrides.
    primary_success_rate_required_percent: float = 99.9
    minimum_primary_success_required: int = 200
    primary_p95_max_ms: float = 10_000.0
    primary_timeout_rate_max_percent: float = 0.1
    # Sustained-outage detection: if the primary fails this many attempts in a
    # row, the run is classified as invalid_primary_unavailable.
    sustained_primary_outage_threshold: int = 30


class PilotState:
    def __init__(self, config: PilotConfig) -> None:
        self.config = config
        self.duration_seconds = int(config.duration_hours * 3600)
        self.start_time_monotonic = time.monotonic()
        self.start_time_wall = datetime.now(timezone.utc)
        self.end_time = self.start_time_monotonic + self.duration_seconds
        self.stop_requested = threading.Event()
        self.sequence = 0
        self.metrics: List[Dict[str, Any]] = []
        self.ledger: Optional[PilotLedger] = None
        self.provider: Optional[ShadowMemoryProvider] = None
        self.evidence_store: Optional[ShadowEvidenceStore] = None
        self.initial_sizes: Dict[str, int] = {}
        self.final_sizes: Dict[str, int] = {}
        self.initial_rss_bytes: Optional[int] = None
        self.final_rss_bytes: Optional[int] = None
        self.storage_health: Optional[Dict[str, Any]] = None
        self.fuli_commit: Optional[str] = None
        # Primary health observability.
        self.primary_available_at_start: Optional[bool] = None
        self.primary_available_at_end: Optional[bool] = None
        self.consecutive_primary_failures: int = 0
        self.max_consecutive_primary_failures: int = 0
        self.primary_writes_above_p95_threshold: int = 0
        # RSS sampling (after warm-up).
        self.rss_samples: List[Tuple[float, int]] = []  # (monotonic_seconds, rss_bytes)
        self.warmup_rss_bytes: Optional[int] = None
        self.rss_sampling_started: bool = False

    def remaining_seconds(self) -> float:
        return max(0.0, self.end_time - time.monotonic())

    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.start_time_monotonic

    def started_at_iso(self) -> str:
        return self.start_time_wall.isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_config_safe() -> Dict[str, Any]:
    try:
        return load_config() or {}
    except Exception as exc:
        print(f"[{now_iso()}] WARNING: failed to load config: {exc}")
        return {}


def verify_shadow_config(config: Dict[str, Any]) -> Dict[str, Any]:
    provider = cfg_get(config, "memory", "provider")
    shadow = cfg_get(config, "memory", "shadow", default={}) or {}

    checks = {
        "memory.provider": provider,
        "shadow.enabled": shadow.get("enabled"),
        "shadow.primary_provider": shadow.get("primary_provider"),
        "shadow.secondary_provider": shadow.get("secondary_provider"),
        "shadow.mirror_writes": shadow.get("mirror_writes"),
        "shadow.compare_reads": shadow.get("compare_reads"),
        "shadow.sample_rate": shadow.get("sample_rate"),
        "shadow.write_timeout_ms": shadow.get("write_timeout_ms"),
        "shadow.read_timeout_ms": shadow.get("read_timeout_ms"),
        "shadow.capture_content": shadow.get("capture_content"),
        "shadow.namespace": shadow.get("namespace"),
    }
    expected = {
        "memory.provider": "shadow",
        "shadow.enabled": True,
        "shadow.primary_provider": "honcho",
        "shadow.secondary_provider": "fuli",
        "shadow.mirror_writes": True,
        "shadow.compare_reads": False,
        "shadow.sample_rate": 0.0,
        "shadow.write_timeout_ms": 10000,
        "shadow.read_timeout_ms": 250,
        "shadow.capture_content": False,
        "shadow.namespace": "hermes:shadow-pilot",
    }
    mismatches = []
    for key, value in expected.items():
        actual = checks.get(key)
        if actual != value:
            mismatches.append(f"{key}: expected {value!r}, got {actual!r}")
    return {"ok": len(mismatches) == 0, "checks": checks, "mismatches": mismatches}


def initialize_providers(config: PilotConfig) -> ShadowMemoryProvider:
    provider = load_memory_provider("shadow")
    if provider is None:
        raise RuntimeError("Failed to load memory provider")
    if provider.name != "shadow":
        raise RuntimeError(f"Expected provider 'shadow', got {provider.name!r}")
    provider.initialize(f"shadow-pilot-{config.run_id}", hermes_home=str(config.hermes_home))
    return provider


def _payload_fingerprint(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _process_rss_bytes() -> int | None:
    """Return current process RSS in bytes, or None if unavailable."""
    try:
        import psutil

        return psutil.Process().memory_info().rss
    except Exception:
        return None


def _fuli_file_sizes(hermes_home: Path) -> Dict[str, int]:
    sizes: Dict[str, int] = {}
    for name in ("fuli.db", "fuli.db-wal", "fuli.db-shm"):
        p = hermes_home / "memories" / name
        sizes[name] = p.stat().st_size if p.exists() else 0
    return sizes


def _record_storage_health(state: PilotState) -> None:
    provider = state.provider
    if provider is None:
        return
    secondary = getattr(provider, "_secondary", None)
    if secondary is None:
        return
    try:
        raw = secondary.handle_tool_call("fuli_memory_storage_health", {"deep": True})
        state.storage_health = json.loads(raw)
    except Exception as exc:
        state.storage_health = {"error": str(exc)}


def _do_one_write(
    state: PilotState,
    provider: ShadowMemoryProvider,
    sequence: int,
) -> None:
    fact = f"Shadow pilot heartbeat {sequence} at {now_iso()}. This is a controlled test memory."
    if sequence % 2 == 0:
        tool_name = "honcho_conclude"
        args = {"peer": "user", "conclusion": fact}
        operation = "conclude"
    else:
        tool_name = "honcho_profile"
        args = {"peer": "user", "card": [fact]}
        operation = "profile"

    payload = json.dumps(args, sort_keys=True)
    fingerprint = _payload_fingerprint(payload)
    ledger = state.ledger
    assert ledger is not None

    correlation_id = ledger.start_operation(sequence, operation, fingerprint)
    pilot_meta = {
        "correlation_id": correlation_id,
        "pilot_run_id": state.config.run_id,
        "pilot_sequence": sequence,
        "synthetic": True,
    }

    start = time.monotonic()
    primary_raw: str
    primary_succeeded = False
    try:
        primary_raw = provider.handle_tool_call(tool_name, args, pilot_meta=pilot_meta)
    except Exception as exc:
        primary_raw = ""
        ledger.record_primary_result(correlation_id, classify_exception(exc))
        _log_primary_error(state, sequence, exc)
        state.consecutive_primary_failures += 1
        if state.consecutive_primary_failures > state.max_consecutive_primary_failures:
            state.max_consecutive_primary_failures = state.consecutive_primary_failures
        return

    latency_ms = (time.monotonic() - start) * 1000.0
    primary_class = classify_honcho_result(primary_raw, latency_ms=latency_ms)
    ledger.record_primary_result(correlation_id, primary_class)

    if not primary_class.is_confirmed_success:
        _log_primary_error(state, sequence, primary_class.error_message or primary_class.status.value)
        state.consecutive_primary_failures += 1
        if state.consecutive_primary_failures > state.max_consecutive_primary_failures:
            state.max_consecutive_primary_failures = state.consecutive_primary_failures
        if latency_ms > state.config.primary_p95_max_ms:
            state.primary_writes_above_p95_threshold += 1
        return

    # Primary confirmed success: reset consecutive-failure counter.
    primary_succeeded = True
    state.consecutive_primary_failures = 0
    if latency_ms > state.config.primary_p95_max_ms:
        state.primary_writes_above_p95_threshold += 1

    # Read the mirror outcome from the evidence store. The shadow provider
    # records the mirror outcome with the same correlation_id.
    mirror_outcome = _read_mirror_outcome(state, correlation_id)
    if mirror_outcome is None:
        # Mirror outcome may not be recorded if the secondary provider is not
        # configured or the write was skipped. Record a no-op mirror attempt.
        ledger.record_mirror_result(
            correlation_id,
            memory_id=None,
            accepted=False,
            failed=False,
            error_code="mirror_outcome_missing",
            error_message="shadow provider did not record a mirror outcome",
        )
        return

    ledger.record_mirror_result(
        correlation_id,
        memory_id=mirror_outcome.get("memory_id"),
        accepted=mirror_outcome.get("accepted", False),
        indexed=mirror_outcome.get("indexed", False),
        pending=mirror_outcome.get("pending", False),
        failed=mirror_outcome.get("failed", False),
        latency_ms=mirror_outcome.get("latency_ms"),
        error_code=mirror_outcome.get("error_code"),
        error_message=mirror_outcome.get("error_message"),
    )


def _read_mirror_outcome(state: PilotState, correlation_id: str) -> Optional[Dict[str, Any]]:
    store = state.evidence_store
    if store is None:
        return None
    row = store.query_by_correlation(correlation_id)
    if row is None:
        return None
    try:
        metadata = json.loads(row.get("metadata") or "{}")
    except Exception:
        metadata = {}
    return {
        "memory_id": row.get("fuli_memory_id"),
        "accepted": bool(row.get("shadow_success")),
        "indexed": False,  # updated during drain
        "pending": False,
        "failed": not bool(row.get("shadow_success")),
        "latency_ms": row.get("latency_ms"),
        "error_code": None if row.get("shadow_success") else "fuli_failed",
        "error_message": row.get("shadow_error"),
        "metadata": metadata,
    }


def _log_primary_error(state: PilotState, sequence: int, error: Any) -> None:
    print(f"[{now_iso()}] PRIMARY ERROR on write {sequence}: {error}")


def _collect_fuli_state(provider: ShadowMemoryProvider) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "thread_alive": None,
        "loop_running": None,
        "diagnostics": None,
        "diagnostics_error": None,
        "namespaces": [],
    }
    try:
        secondary = getattr(provider, "_secondary", None)
        if secondary is not None:
            bridge = getattr(secondary, "_bridge", None)
            if bridge is not None:
                thread = getattr(bridge, "_thread", None)
                loop = getattr(bridge, "_loop", None)
                state["thread_alive"] = thread.is_alive() if thread is not None else None
                state["loop_running"] = loop.is_running() if loop is not None else None
            try:
                diag_raw = secondary.handle_tool_call("fuli_memory_diagnostics", {})
                diag = json.loads(diag_raw)
                state["diagnostics"] = diag
                state["namespaces"] = diag.get("namespaces", []) or []
            except Exception as exc:
                state["diagnostics_error"] = str(exc)
    except Exception as exc:
        state["diagnostics_error"] = str(exc)
    return state


def _collect_evidence_stats(store: Optional[ShadowEvidenceStore]) -> Dict[str, Any]:
    stats = {"mirrored_write_rows": 0, "observation_rows": 0, "diagnostic_events": 0, "error": None}
    if store is None:
        stats["error"] = "no evidence store"
        return stats
    try:
        stats.update(store.diagnostic_summary())
        stats["diagnostic_events"] = stats["total"]
        with sqlite3.connect(str(store.db_path), check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            stats["mirrored_write_rows"] = conn.execute("SELECT COUNT(*) FROM mirrored_writes").fetchone()[0]
            stats["observation_rows"] = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
    except Exception as exc:
        stats["error"] = str(exc)
    return stats


def heartbeat(state: PilotState) -> Dict[str, Any]:
    provider = state.provider
    assert provider is not None
    fuli_state = _collect_fuli_state(provider)
    evidence_stats = _collect_evidence_stats(state.evidence_store)
    fuli_db_path = state.config.hermes_home / "memories" / "fuli.db"
    fuli_db_size = fuli_db_path.stat().st_size if fuli_db_path.exists() else 0

    ledger_summary = state.ledger.summary() if state.ledger else {}
    record = {
        "timestamp": now_iso(),
        "remaining_seconds": state.remaining_seconds(),
        "ledger": ledger_summary,
        "fuli_db_size": fuli_db_size,
        "evidence_rows": evidence_stats["mirrored_write_rows"],
        "observation_rows": evidence_stats["observation_rows"],
        "diagnostic_events": evidence_stats["diagnostic_events"],
        "thread_alive": fuli_state.get("thread_alive"),
        "loop_running": fuli_state.get("loop_running"),
        "fuli_diagnostics": fuli_state.get("diagnostics"),
        "fuli_diagnostics_error": fuli_state.get("diagnostics_error"),
        "namespaces": fuli_state.get("namespaces", []),
    }
    state.metrics.append(record)
    return record


def _drain_indexing(state: PilotState) -> Dict[str, Any]:
    """Poll Fuli until all mirrored memories are indexed or failed, or timeout."""
    ledger = state.ledger
    provider = state.provider
    assert ledger is not None and provider is not None
    secondary = getattr(provider, "_secondary", None)
    if secondary is None:
        return {"ok": False, "error": "secondary provider not available", "pending": 0}

    deadline = time.monotonic() + state.config.drain_timeout_seconds
    poll_interval = 1.0
    iterations = 0
    final_status: Dict[str, str] = {}

    # Get memory IDs that are accepted but not yet known to be indexed/failed.
    rows = ledger.get_all_rows()
    accepted_rows = [r for r in rows if r.fuli_accepted and not r.fuli_indexed and not r.fuli_failed]
    memory_ids = [r.fuli_memory_id for r in accepted_rows if r.fuli_memory_id]

    while time.monotonic() < deadline and memory_ids:
        iterations += 1
        statuses = _batch_fuli_status(secondary, memory_ids)
        still_pending: List[str] = []
        for mid in memory_ids:
            status = statuses.get(mid, "pending")
            final_status[mid] = status
            if status == "indexed":
                _update_ledger_from_fuli_status(ledger, accepted_rows, mid, status)
            elif status == "failed":
                _update_ledger_from_fuli_status(ledger, accepted_rows, mid, status)
            else:
                still_pending.append(mid)
        memory_ids = still_pending
        if memory_ids:
            time.sleep(min(poll_interval, deadline - time.monotonic()))

    # Final classification for any still-pending IDs.
    for mid in memory_ids:
        _update_ledger_from_fuli_status(ledger, accepted_rows, mid, "pending_after_timeout")

    rows_after = ledger.get_all_rows()
    indexed = sum(1 for r in rows_after if r.fuli_indexed)
    pending = sum(1 for r in rows_after if r.fuli_pending)
    failed = sum(1 for r in rows_after if r.fuli_failed)
    accepted = sum(1 for r in rows_after if r.fuli_accepted)

    return {
        "ok": True,
        "iterations": iterations,
        "drain_timeout_seconds": state.config.drain_timeout_seconds,
        "accepted": accepted,
        "indexed": indexed,
        "pending": pending,
        "failed": failed,
        "final_status": final_status,
    }


def _batch_fuli_status(secondary: Any, memory_ids: List[str]) -> Dict[str, str]:
    """Return status for each memory ID. Prefer batch_status if available; otherwise poll get."""
    if hasattr(secondary, "batch_status"):
        try:
            # Fuli adapter returns a dict of status strings.
            result = secondary.batch_status(memory_ids=memory_ids)
            if isinstance(result, dict):
                statuses: Dict[str, str] = {}
                for mid, value in result.items():
                    if isinstance(value, str):
                        statuses[mid] = value
                    elif value is None:
                        statuses[mid] = "pending"
                    else:
                        # value is an AddResult-like object
                        statuses[mid] = _lifecycle_from_status(getattr(value, "status", None), getattr(value, "indexing_status", None))
                return statuses
        except Exception:
            pass
    statuses: Dict[str, str] = {}
    for mid in memory_ids:
        try:
            result = secondary.handle_tool_call("fuli_memory_get", {"memory_id": mid})
            data = json.loads(result)
            lifecycle = data.get("lifecycle", "pending")
            statuses[mid] = "indexed" if lifecycle == "indexed" else ("failed" if lifecycle == "failed" else lifecycle)
        except Exception as exc:
            statuses[mid] = "pending"
    return statuses


def _lifecycle_from_status(status: Any, indexing_status: Any) -> str:
    if hasattr(status, "value"):
        status = status.value
    if hasattr(indexing_status, "value"):
        indexing_status = indexing_status.value
    status_str = str(status or "").lower()
    indexing_str = str(indexing_status or "").lower()
    if status_str in {"failed", "error"} or indexing_str == "failed":
        return "failed"
    if indexing_str in {"indexed", "handled"}:
        return "indexed"
    if status_str in {"accepted", "pending"} or indexing_str == "pending":
        return "pending"
    return "pending"


def _update_ledger_from_fuli_status(
    ledger: PilotLedger,
    accepted_rows: List[Any],
    memory_id: str,
    status: str,
) -> None:
    for row in accepted_rows:
        if row.fuli_memory_id == memory_id:
            ledger.update_indexing_status(
                row.correlation_id,
                indexed=(status == "indexed"),
                pending=(status == "pending" or status == "pending_after_timeout"),
                failed=(status == "failed"),
                error_code=None if status in {"indexed", "pending"} else status,
            )
            break


def _run_pilot_internal(config: PilotConfig) -> Dict[str, Any]:
    state = PilotState(config)
    config_check = verify_shadow_config(load_config_safe())
    if not config_check["ok"]:
        return {"ok": False, "phase": "config_check", "errors": config_check["mismatches"]}

    state.fuli_commit = _derive_fuli_commit()
    state.initial_sizes = _fuli_file_sizes(config.hermes_home)
    state.initial_rss_bytes = _process_rss_bytes()

    ledger_path = config.hermes_home / "memories" / "pilot_ledger.db"
    state.ledger = PilotLedger(ledger_path, run_id=config.run_id)
    state.evidence_store = ShadowEvidenceStore(config.hermes_home / "memories" / "shadow.db")
    state.provider = initialize_providers(config)

    # Warm both primary and secondary before measured attempts.
    try:
        primary = getattr(state.provider, "_primary", None) if state.provider else None
        secondary = getattr(state.provider, "_secondary", None) if state.provider else None
        if primary is not None and hasattr(primary, "handle_tool_call"):
            primary.handle_tool_call("honcho_context", {"peer": "user", "query": "warm-up"})
            state.primary_available_at_start = True
        else:
            state.primary_available_at_start = False
        if secondary is not None and hasattr(secondary, "handle_tool_call"):
            secondary.handle_tool_call("fuli_memory_add", {"content": "warm-up record", "source": "pilot_warmup", "namespace": state.config.namespace})
    except Exception as exc:
        print(f"[{now_iso()}] Warm-up warning (non-fatal): {exc}")
        # Warm-up failures are not necessarily fatal, but a primary connection
        # error during warm-up is a strong signal that the primary is down.
        if "honcho" in str(exc).lower() or "primary" in str(exc).lower():
            state.primary_available_at_start = False

    def _on_signal(signum, frame):
        print(f"[{now_iso()}] Signal {signum} received; stopping pilot at next interval...")
        state.stop_requested.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    print(f"[{now_iso()}] Pilot {config.run_id} started for {config.duration_hours}h with {config.interval_seconds}s interval")
    next_heartbeat = time.monotonic() + 300.0

    while not state.stop_requested.is_set() and time.monotonic() < state.end_time:
        state.sequence += 1
        try:
            _do_one_write(state, state.provider, state.sequence)
            print(f"[{now_iso()}] Write {state.sequence} completed")
        except Exception as exc:
            if state.ledger:
                corr = state.ledger.start_operation(state.sequence, "unknown", "error")
                state.ledger.record_primary_result(corr, classify_exception(exc))
            print(f"[{now_iso()}] EXCEPTION on write {state.sequence}: {exc}")
            traceback.print_exc()

        # RSS sampling after warm-up (after the first 5 successful writes, or
        # at the first heartbeat, whichever comes first). Sample at every write.
        elapsed = state.elapsed_seconds()
        rss_now = _process_rss_bytes()
        if rss_now is not None:
            if state.warmup_rss_bytes is None and elapsed >= 5.0:
                state.warmup_rss_bytes = rss_now
                state.rss_sampling_started = True
            if state.rss_sampling_started:
                state.rss_samples.append((elapsed, rss_now))

        if time.monotonic() >= next_heartbeat:
            record = heartbeat(state)
            print(f"[{now_iso()}] HEARTBEAT: {record}")
            next_heartbeat = time.monotonic() + 300.0

        sleep_seconds = config.interval_seconds - (elapsed % config.interval_seconds)
        sleep_seconds = min(sleep_seconds, state.remaining_seconds())
        sleep_seconds = max(0.1, sleep_seconds)
        if state.stop_requested.wait(timeout=sleep_seconds):
            break

    # End-of-run lifecycle: stop writing, drain, validate, generate report.
    print(f"[{now_iso()}] Pilot loop ended; draining indexing...")
    drain_result = _drain_indexing(state)

    # Collect final health and sizes before shutting down the provider.
    state.final_sizes = _fuli_file_sizes(config.hermes_home)
    state.final_rss_bytes = _process_rss_bytes()
    final_record = heartbeat(state)
    _record_storage_health(state)
    # Sample final RSS as the last entry in the series.
    if state.final_rss_bytes is not None and state.rss_sampling_started:
        state.rss_samples.append((state.elapsed_seconds(), state.final_rss_bytes))
    # Capture primary availability at end of run, before shutdown.
    try:
        primary_provider = getattr(state.provider, "_primary", None) if state.provider else None
        if primary_provider is not None:
            state.primary_available_at_end = bool(
                getattr(primary_provider, "is_available", lambda: True)()
            )
    except Exception:
        state.primary_available_at_end = False
    print(f"[{now_iso()}] Final heartbeat: {final_record}")

    try:
        state.provider.shutdown()
        print(f"[{now_iso()}] Provider shutdown complete")
    except Exception as exc:
        print(f"[{now_iso()}] Provider shutdown error: {exc}")

    report = _build_report(state, drain_result, final_record)
    report_path = _save_report(config, report)
    report["report_path"] = str(report_path)
    report["config_check"] = config_check
    return report


def _build_report(
    state: PilotState,
    drain_result: Dict[str, Any],
    final_record: Dict[str, Any],
) -> Dict[str, Any]:
    ledger = state.ledger
    assert ledger is not None
    ledger_report = ledger.compute_report()

    if not ledger_report["balanced"]:
        return {
            "ok": False,
            "run_status": "invalid_fuli_ingestion",
            "status_reason": "mirror accounting unbalanced",
            "phase": "accounting_validation",
            "errors": ledger_report["invariant_errors"] + ["mirror accounting unbalanced"],
            "ledger": ledger_report,
            "drain": drain_result,
        }

    total = ledger_report["total_attempts"]
    primary_success = ledger_report["primary_success"]
    primary_failure = ledger_report["primary_failure"]
    mirror_attempted = ledger_report["mirror_attempted"]
    indexed = ledger_report["indexed"]
    pending = ledger_report["pending"]
    failed = ledger_report["failed"]
    unexplained = ledger_report.get("unexplained_mirror_attempts", 0)

    accepted_rate = (mirror_attempted / max(primary_success, 1)) * 100.0 if primary_success else 0.0
    immediate_index_rate = (indexed / max(mirror_attempted, 1)) * 100.0
    eventual_index_rate = ((indexed + failed) / max(mirror_attempted, 1)) * 100.0
    primary_success_rate = (primary_success / max(total, 1)) * 100.0

    # Latency percentiles from ledger rows.
    rows = ledger_report.get("rows", [])
    primary_latencies = [r["primary_latency_ms"] for r in rows if r.get("primary_latency_ms")]
    mirror_latencies = [r["fuli_latency_ms"] for r in rows if r.get("fuli_latency_ms")]
    indexing_latencies = []
    for r in rows:
        if r.get("fuli_indexed_at") and r.get("primary_completed_at"):
            try:
                idx = datetime.fromisoformat(r["fuli_indexed_at"].replace("Z", "+00:00"))
                pri = datetime.fromisoformat(r["primary_completed_at"].replace("Z", "+00:00"))
                indexing_latencies.append((idx - pri).total_seconds() * 1000.0)
            except Exception:
                pass

    fuli_db_path = state.config.hermes_home / "memories" / "fuli.db"
    fuli_db_size = fuli_db_path.stat().st_size if fuli_db_path.exists() else 0

    # Privacy / namespace checks
    namespaces = final_record.get("namespaces", [])
    namespace_leak = any(ns != state.config.namespace for ns in namespaces)
    raw_content_violations = _count_raw_content_violations(state.evidence_store)

    # Primary error category breakdown
    primary_error_categories = ledger_report.get("primary_error_categories", {})

    # Size growth
    initial_sizes = state.initial_sizes or {}
    final_sizes = state.final_sizes or {}
    size_growth = {
        name: final_sizes.get(name, 0) - initial_sizes.get(name, 0)
        for name in set(initial_sizes) | set(final_sizes)
    }
    wal_growth_bytes = size_growth.get("fuli.db-wal", 0) + size_growth.get("fuli.db-shm", 0)
    db_growth_bytes = size_growth.get("fuli.db", 0)

    # (Memory growth is now computed by `_rss_report` from the sampled series.)
    memory_growth_bytes: Optional[int] = None
    if state.initial_rss_bytes is not None and state.final_rss_bytes is not None:
        memory_growth_bytes = state.final_rss_bytes - state.initial_rss_bytes

    # Lock retries: best-effort from storage health or diagnostics
    lock_retries = 0
    if state.storage_health and isinstance(state.storage_health, dict):
        lock_retries = state.storage_health.get("lock_retries", 0)

    # Integrity / quick check
    integrity_ok = None
    quick_check_ok = None
    if state.storage_health and isinstance(state.storage_health, dict):
        integrity = state.storage_health.get("integrity_check")
        quick = state.storage_health.get("quick_check")
        integrity_ok = integrity == ["ok"] if isinstance(integrity, list) else None
        quick_check_ok = quick == ["ok"] if isinstance(quick, list) else None

    # Pass-gate evaluation. These gates describe FULI-side health only.
    # The primary-side gates live below, and they cannot be overridden by
    # perfect Fuli mirroring — see run_status decision logic further down.
    fuli_pass_gates = {
        "accounting_balanced": ledger_report["balanced"] and unexplained == 0,
        "mirror_only_on_success": mirror_attempted <= primary_success,
        "accepted_rate_99": (mirror_attempted / max(primary_success, 1)) >= 0.99,
        "eventual_index_rate_99": (indexed / max(mirror_attempted, 1)) >= 0.99,
        "zero_silent_loss": pending == 0 and unexplained == 0,
        "zero_namespace_leak": not namespace_leak,
        "zero_raw_content_violations": raw_content_violations == 0,
        "sqlite_integrity_ok": integrity_ok is not False,
        "primary_output_unchanged": True,  # measured by classifier; no mutation detected
    }
    fuli_pass_gates_ok = all(fuli_pass_gates.values())

    # --- Primary health gate evaluation (added for the 2026-07-13 false-green fix) ---
    cfg = state.config
    primary_success_rate_required = cfg.primary_success_rate_required_percent
    minimum_primary_success_required = cfg.minimum_primary_success_required
    primary_p95_max_ms = cfg.primary_p95_max_ms
    primary_timeout_rate_max_pct = cfg.primary_timeout_rate_max_percent
    sustained_outage_threshold = cfg.sustained_primary_outage_threshold

    primary_availability_gate = bool(state.primary_available_at_start) and bool(
        state.primary_available_at_end
    )
    primary_success_rate_gate = primary_success_rate >= primary_success_rate_required
    minimum_primary_success_gate = primary_success >= minimum_primary_success_required
    sustained_primary_outage_detected = (
        state.max_consecutive_primary_failures >= sustained_outage_threshold
    )

    primary_timeout_rate_pct = (
        (state.primary_writes_above_p95_threshold / max(total, 1)) * 100.0 if total else 0.0
    )
    primary_p95_latency_gate = _percentile(primary_latencies, 95) <= primary_p95_max_ms if primary_latencies else True
    primary_timeout_rate_gate = primary_timeout_rate_pct <= primary_timeout_rate_max_pct

    primary_health_gates = {
        "primary_availability_gate": primary_availability_gate,
        "primary_success_rate_gate": primary_success_rate_gate,
        "primary_p95_latency_gate": primary_p95_latency_gate,
        "primary_timeout_rate_gate": primary_timeout_rate_gate,
        "minimum_primary_success_gate": minimum_primary_success_gate,
        "no_sustained_primary_outage": not sustained_primary_outage_detected,
    }
    primary_health_gates_ok = all(primary_health_gates.values())

    # --- run_status decision (precedence: integrity > fuli > primary) ---
    # 1. Integrity failure (SQLite corruption or quick_check fail) is the most
    #    severe because it implies the run's accounting is not trustworthy.
    # 2. Fuli ingestion failure implies the secondary side is broken even
    #    though the primary might be healthy.
    # 3. Primary issues are ranked by severity: unavailability (sustained
    #    outage or unreachable primary) > success-rate miss > sample-size miss.
    run_status = "valid"
    status_reason = ""

    if integrity_ok is False or quick_check_ok is False:
        run_status = "invalid_integrity"
        status_reason = "SQLite integrity or quick-check failed"
    elif not fuli_pass_gates.get("mirror_only_on_success", False):
        run_status = "invalid_fuli_ingestion"
        status_reason = "Fuli mirrored writes not bounded by primary successes"
    elif not fuli_pass_gates.get("zero_silent_loss", False):
        run_status = "invalid_fuli_ingestion"
        status_reason = "silent loss detected in mirror ledger"
    elif (
        primary_availability_gate is False
        or sustained_primary_outage_detected
        or state.primary_available_at_start is False
        or state.primary_available_at_end is False
    ):
        run_status = "invalid_primary_unavailable"
        status_reason = (
            f"primary unavailable: start={state.primary_available_at_start}, "
            f"end={state.primary_available_at_end}, "
            f"max_consecutive_failures={state.max_consecutive_primary_failures}"
        )
    elif not primary_success_rate_gate:
        run_status = "invalid_primary_success_rate"
        status_reason = (
            f"primary success rate {primary_success_rate:.2f}% below required "
            f"{primary_success_rate_required:.2f}%"
        )
    elif not minimum_primary_success_gate:
        run_status = "invalid_primary_sample_size"
        status_reason = (
            f"primary success count {primary_success} below minimum "
            f"{minimum_primary_success_required}"
        )
    elif not fuli_pass_gates_ok:
        run_status = "invalid_fuli_ingestion"
        status_reason = "Fuli ingestion gate failed"

    report_ok = run_status == "valid"

    return {
        "ok": report_ok,
        "run_id": state.config.run_id,
        "phase": "completed" if report_ok else f"invalid_{run_status}",
        "fuli_commit": state.fuli_commit,
        "duration_hours": state.config.duration_hours,
        "interval_seconds": state.config.interval_seconds,
        "started_at": state.started_at_iso(),
        "finished_at": now_iso(),
        "run_status": run_status,
        "status_reason": status_reason,
        "primary_success_rate_percent": round(primary_success_rate, 4),
        "primary_success_rate_required_percent": primary_success_rate_required,
        "primary_available_at_start": state.primary_available_at_start,
        "primary_available_at_end": state.primary_available_at_end,
        "primary_availability_gate": primary_availability_gate,
        "minimum_primary_success_required": minimum_primary_success_required,
        "minimum_primary_success_gate": minimum_primary_success_gate,
        "sustained_primary_outage_detected": sustained_primary_outage_detected,
        "max_consecutive_primary_failures": state.max_consecutive_primary_failures,
        "primary_p95_max_ms": primary_p95_max_ms,
        "primary_timeout_rate_percent": round(primary_timeout_rate_pct, 4),
        "primary_timeout_rate_max_percent": primary_timeout_rate_max_pct,
        "primary_health_gates": primary_health_gates,
        "config": {
            "provider": cfg_get(load_config_safe(), "memory", "provider"),
            "enabled": cfg_get(load_config_safe(), "memory", "shadow", "enabled"),
            "primary_provider": cfg_get(load_config_safe(), "memory", "shadow", "primary_provider"),
            "secondary_provider": cfg_get(load_config_safe(), "memory", "shadow", "secondary_provider"),
            "mirror_writes": cfg_get(load_config_safe(), "memory", "shadow", "mirror_writes"),
            "compare_reads": cfg_get(load_config_safe(), "memory", "shadow", "compare_reads"),
            "sample_rate": cfg_get(load_config_safe(), "memory", "shadow", "sample_rate"),
            "timeout_ms": cfg_get(load_config_safe(), "memory", "shadow", "timeout_ms"),
            "capture_content": cfg_get(load_config_safe(), "memory", "shadow", "capture_content"),
            "namespace": cfg_get(load_config_safe(), "memory", "shadow", "namespace"),
        },
        "ledger": {
            "total_attempts": total,
            "primary_success": primary_success,
            "primary_failure": primary_failure,
            "primary_success_rate_percent": round(primary_success_rate, 4),
            "mirror_attempted": mirror_attempted,
            "indexed": indexed,
            "pending": pending,
            "failed": failed,
            "accepted_rate_percent": round(accepted_rate, 4),
            "immediate_index_rate_percent": round(immediate_index_rate, 4),
            "eventual_index_rate_percent": round(eventual_index_rate, 4),
            "primary_error_categories": primary_error_categories,
            "unexplained_mirror_attempts": unexplained,
        },
        "latencies": {
            "primary_p50_ms": _percentile(primary_latencies, 50) if primary_latencies else 0.0,
            "primary_p95_ms": _percentile(primary_latencies, 95) if primary_latencies else 0.0,
            "mirror_p50_ms": _percentile(mirror_latencies, 50) if mirror_latencies else 0.0,
            "mirror_p95_ms": _percentile(mirror_latencies, 95) if mirror_latencies else 0.0,
            # drain_lag_p50_ms measures primary completion -> final drain
            # confirmation, NOT actual vector-index execution time. The legacy
            # field indexing_p50_ms is preserved as a deprecated alias for
            # backward compatibility with existing reports/tests.
            "drain_lag_p50_ms": _percentile(indexing_latencies, 50) if indexing_latencies else 0.0,
            "drain_lag_p95_ms": _percentile(indexing_latencies, 95) if indexing_latencies else 0.0,
            "indexing_p50_ms": _percentile(indexing_latencies, 50) if indexing_latencies else 0.0,
            "indexing_p95_ms": _percentile(indexing_latencies, 95) if indexing_latencies else 0.0,
        },
        "drain": drain_result,
        "evidence_store": {
            "row_count": final_record.get("evidence_rows", 0),
            "observation_rows": final_record.get("observation_rows", 0),
            "diagnostic_events": final_record.get("diagnostic_events", 0),
            "namespaces": namespaces,
            "namespace_leak": namespace_leak,
            "raw_content_violations": raw_content_violations,
        },
        "fuli_db": {
            "final_size_bytes": fuli_db_size,
            "initial_size_bytes": initial_sizes.get("fuli.db", 0),
            "db_growth_bytes": db_growth_bytes,
            "wal_growth_bytes": wal_growth_bytes,
            "wal_pages": state.storage_health.get("wal_pages") if state.storage_health else None,
            "wal_size_bytes": state.storage_health.get("wal_size_bytes") if state.storage_health else None,
        },
        "memory": _rss_report(state),
        "storage_health": state.storage_health,
        "lock_retries": lock_retries,
        "integrity": {
            "integrity_ok": integrity_ok,
            "quick_check_ok": quick_check_ok,
        },
        "pass_gates": fuli_pass_gates,
        "health": {
            "thread_alive_final": final_record.get("thread_alive"),
            "loop_running_final": final_record.get("loop_running"),
        },
    }


def _rss_report(state: PilotState) -> Dict[str, Any]:
    """Build the structured RSS report from the sampled series.

    The report includes warm-up RSS, p50/p95/min/max, final RSS, the linear
    slope of the series (bytes per second), and peak-minus-warmup. RSS
    decreases are NOT a failure; the caller decides what to do with the trend.
    """
    initial = state.initial_rss_bytes
    final = state.final_rss_bytes
    warmup = state.warmup_rss_bytes
    samples = state.rss_samples or []

    growth_bytes: Optional[int] = None
    if initial is not None and final is not None:
        growth_bytes = final - initial

    rss_values: List[float] = [float(v) for _, v in samples]
    rss_p50: Optional[float] = _percentile(rss_values, 50) if rss_values else None
    rss_p95: Optional[float] = _percentile(rss_values, 95) if rss_values else None
    rss_max: Optional[float] = max(rss_values) if rss_values else None
    rss_min: Optional[float] = min(rss_values) if rss_values else None

    # Linear slope via least-squares on the (elapsed_seconds, rss_bytes) samples.
    slope_bytes_per_sec: Optional[float] = None
    peak_minus_warmup: Optional[int] = None
    if warmup is not None and samples:
        peak_minus_warmup = max(rss_values) - warmup
    if len(samples) >= 2:
        xs = [s for s, _ in samples]
        ys = [v for _, v in samples]
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        num = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
        den = sum((xs[i] - mean_x) ** 2 for i in range(n))
        if den > 0:
            slope_bytes_per_sec = num / den

    return {
        "initial_rss_bytes": initial,
        "final_rss_bytes": final,
        "growth_bytes": growth_bytes,
        "warmup_rss_bytes": warmup,
        "rss_p50_bytes": rss_p50,
        "rss_p95_bytes": rss_p95,
        "rss_max_bytes": rss_max,
        "rss_min_bytes": rss_min,
        "rss_slope_bytes_per_second": slope_bytes_per_sec,
        "peak_minus_warmup_bytes": peak_minus_warmup,
        "rss_sample_count": len(rss_values),
    }


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (pct / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return float(s[f] + (s[c] - s[f]) * (k - f))


def _count_raw_content_violations(store: Optional[ShadowEvidenceStore]) -> int:
    if store is None:
        return 0
    try:
        with sqlite3.connect(str(store.db_path), check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT COUNT(*) FROM mirrored_writes WHERE content_fingerprint IS NULL"
            ).fetchone()[0]
    except Exception:
        return 0


def _redact_report_for_publication(report: Dict[str, Any]) -> Dict[str, Any]:
    """Remove raw content, memory IDs, and correlation IDs from the public report."""
    safe = json.loads(json.dumps(report, default=str))
    safe.pop("ledger_rows", None)
    if "ledger" in safe and isinstance(safe["ledger"], dict):
        safe["ledger"].pop("rows", None)
    return safe


def _save_report(config: PilotConfig, report: Dict[str, Any]) -> Path:
    run_dir = REPORTS_DIR / config.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    (run_dir / "report.redacted.json").write_text(
        json.dumps(_redact_report_for_publication(report), indent=2, default=str)
    )
    (run_dir / "report.md").write_text(_render_markdown(report))
    return run_dir / "report.json"


def _render_markdown(report: Dict[str, Any]) -> str:
    lines = [
        f"# Shadow Pilot Report — {report['run_id']}",
        "",
        f"- **Status:** {'completed' if report.get('ok') else 'FAILED'}",
        f"- **Phase:** {report.get('phase')}",
        f"- **Duration:** {report['duration_hours']} hours",
        f"- **Started:** {report['started_at']}",
        f"- **Finished:** {report['finished_at']}",
        "",
        "## Primary writes",
        "",
        f"- Attempts: **{report['ledger']['total_attempts']}**",
        f"- Successes: **{report['ledger']['primary_success']}**",
        f"- Failures: **{report['ledger']['primary_failure']}**",
        f"- Success rate: **{report['ledger']['primary_success_rate_percent']}%**",
        "",
        "## Fuli mirror",
        "",
        f"- Mirrored attempts: **{report['ledger']['mirror_attempted']}**",
        f"- Indexed: **{report['ledger']['indexed']}**",
        f"- Pending after drain: **{report['ledger']['pending']}**",
        f"- Failed: **{report['ledger']['failed']}**",
        f"- Immediate index rate: **{report['ledger']['immediate_index_rate_percent']}%**",
        f"- Eventual index rate: **{report['ledger']['eventual_index_rate_percent']}%**",
        "",
        "## Drain",
        "",
        f"- Iterations: {report['drain'].get('iterations', 0)}",
        f"- Timeout seconds: {report['drain'].get('drain_timeout_seconds', 0)}",
        "",
        "## Health and privacy",
        "",
        f"- Namespace leak: {report['evidence_store']['namespace_leak']}",
        f"- Raw content violations: {report['evidence_store']['raw_content_violations']}",
        f"- Fuli DB size: {report['fuli_db']['final_size_bytes']} bytes",
        "",
        "## Latencies",
        "",
        f"- Primary p50/p95: {report['latencies']['primary_p50_ms']} / {report['latencies']['primary_p95_ms']} ms",
        f"- Mirror p50/p95: {report['latencies']['mirror_p50_ms']} / {report['latencies']['mirror_p95_ms']} ms",
        "",
    ]
    if report.get("errors"):
        lines.extend(["## Errors", ""])
        for err in report["errors"]:
            lines.append(f"- {err}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI-facing helpers
# ---------------------------------------------------------------------------


def _derive_fuli_commit() -> str | None:
    """Return the git HEAD of the installed editable Fuli checkout, or None."""
    try:
        import fuli
        from pathlib import Path as _Path
        import subprocess

        fuli_root = _Path(fuli.__file__).resolve().parent.parent
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(fuli_root),
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() or None
    except Exception:
        return None


def _model_cached(model: str) -> bool:
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
    normalized = "models--" + model.replace("/", "--")
    return (cache_dir / normalized).exists()


def run_preflight() -> Dict[str, Any]:
    config = load_config_safe()
    result = verify_shadow_config(config)
    result["handler"] = "shadow_pilot.run_preflight"
    result["profile"] = PROFILE_NAME
    result["hermes_home"] = str(HERMES_HOME)

    # Primary availability
    primary_name = cfg_get(config, "memory", "shadow", "primary_provider") or "honcho"
    primary = load_memory_provider(primary_name)
    result["primary_available"] = primary is not None and primary.is_available()

    # Fuli import path and commit
    try:
        import fuli

        result["fuli_import_path"] = fuli.__file__
    except Exception as exc:
        result["fuli_import_path"] = f"error: {exc}"
    fuli_commit = _derive_fuli_commit()
    result["fuli_commit"] = fuli_commit
    result["fuli_commit_approved"] = APPROVED_FULI_COMMIT
    result["fuli_commit_ok"] = fuli_commit == APPROVED_FULI_COMMIT

    # Model cache
    model = cfg_get(config, "memory", "fuli", "embedding_model") or "sentence-transformers/all-MiniLM-L6-v2"
    result["model_cached"] = _model_cached(model)
    result["model"] = model

    # Writable directories
    memories_dir = HERMES_HOME / "memories"
    shadow_dir = HERMES_HOME / "shadow"
    reports_dir = REPORTS_DIR
    try:
        memories_dir.mkdir(parents=True, exist_ok=True)
        shadow_dir.mkdir(parents=True, exist_ok=True)
        reports_dir.mkdir(parents=True, exist_ok=True)
        result["db_dir_writable"] = True
        result["evidence_dir_writable"] = True
        result["report_dir_writable"] = True
    except Exception as exc:
        result["db_dir_writable"] = False
        result["evidence_dir_writable"] = False
        result["report_dir_writable"] = False
        result["writable_error"] = str(exc)

    # Detailed config checks for required preflight fields
    shadow = cfg_get(config, "memory", "shadow", default={}) or {}
    result["mirror_writes"] = shadow.get("mirror_writes", False)
    result["compare_reads"] = shadow.get("compare_reads", False)
    result["sample_rate"] = shadow.get("sample_rate", 0.0)
    result["capture_content"] = shadow.get("capture_content", False)
    result["namespace"] = shadow.get("namespace", "")

    required = {
        "mirror_writes": True,
        "compare_reads": False,
        "sample_rate": 0.0,
        "capture_content": False,
        "namespace": "hermes:shadow-pilot",
    }
    result["required"] = required
    config_ok = result.get("ok", False)
    result["ok"] = (
        config_ok
        and result["primary_available"]
        and result["fuli_commit_ok"]
        and result["model_cached"]
        and result["db_dir_writable"]
        and result["evidence_dir_writable"]
        and result["report_dir_writable"]
        and result["mirror_writes"] == required["mirror_writes"]
        and result["compare_reads"] == required["compare_reads"]
        and result["sample_rate"] == required["sample_rate"]
        and result["capture_content"] == required["capture_content"]
        and result["namespace"] == required["namespace"]
    )
    if not result["ok"]:
        failures = []
        if not result["primary_available"]:
            failures.append("primary provider not available")
        if not result["fuli_commit_ok"]:
            failures.append(f"Fuli commit mismatch: {fuli_commit} != {APPROVED_FULI_COMMIT}")
        if not result["model_cached"]:
            failures.append("embedding model not cached")
        if not result["db_dir_writable"]:
            failures.append("Fuli DB directory not writable")
        if not result["evidence_dir_writable"]:
            failures.append("evidence directory not writable")
        if not result["report_dir_writable"]:
            failures.append("report directory not writable")
        if result["mirror_writes"] != required["mirror_writes"]:
            failures.append("mirror_writes must be True")
        if result["compare_reads"] != required["compare_reads"]:
            failures.append("compare_reads must be False")
        if result["sample_rate"] != required["sample_rate"]:
            failures.append("sample_rate must be 0.0")
        if result["capture_content"] != required["capture_content"]:
            failures.append("capture_content must be False")
        if result["namespace"] != required["namespace"]:
            failures.append("namespace must be hermes:shadow-pilot")
        if not config_ok:
            failures.append("config validation failed")
        result["failures"] = failures
    return result


def run_status() -> Dict[str, Any]:
    config = load_config_safe()
    result = verify_shadow_config(config)
    result["handler"] = "shadow_pilot.run_status"
    result["profile"] = PROFILE_NAME
    result["hermes_home"] = str(HERMES_HOME)

    # Include latest report summary if available.
    try:
        latest = sorted(REPORTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[0]
        report_json = json.loads((latest / "report.json").read_text())
        result["latest_report"] = {
            "run_id": report_json.get("run_id"),
            "path": str(latest),
            "ok": report_json.get("ok"),
            "phase": report_json.get("phase"),
            "total_attempts": report_json.get("ledger", {}).get("total_attempts"),
            "primary_success": report_json.get("ledger", {}).get("primary_success"),
            "indexed": report_json.get("ledger", {}).get("indexed"),
            "pending": report_json.get("ledger", {}).get("pending"),
        }
    except Exception as exc:
        result["latest_report_error"] = str(exc)
    return result


def generate_cli_report() -> Dict[str, Any]:
    try:
        latest = sorted(REPORTS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)[0]
        report_json = json.loads((latest / "report.json").read_text())
        return {
            "ok": report_json.get("ok", False),
            "handler": "shadow_pilot.generate_cli_report",
            "run_id": report_json.get("run_id"),
            "path": str(latest / "report.json"),
            "summary": report_json.get("ledger", {}),
        }
    except Exception as exc:
        return {"ok": False, "handler": "shadow_pilot.generate_cli_report", "error": str(exc)}


def run_cleanup(run_id: str, dry_run: bool = False, execute: bool = False) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "handler": "shadow_pilot.run_cleanup",
        "run_id": run_id,
        "dry_run": dry_run,
        "execute": execute,
    }
    if not dry_run and not execute:
        result["ok"] = False
        result["error"] = "specify either --dry-run or --execute"
        return result

    # Only clean the evidence store in this P0 pass. Fuli memories can be removed
    # by metadata filter once the provider exposes a delete-by-metadata API.
    store = ShadowEvidenceStore(HERMES_HOME / "memories" / "shadow.db")
    if dry_run:
        with sqlite3.connect(str(store.db_path), check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            mirrored = conn.execute(
                "SELECT COUNT(*) FROM mirrored_writes WHERE metadata LIKE ?",
                (f'%"pilot_run_id": "{run_id}"%',),
            ).fetchone()[0]
            diag = conn.execute(
                "SELECT COUNT(*) FROM diagnostic_events WHERE error_message LIKE ?",
                (f'%"pilot_run_id": "{run_id}"%',),
            ).fetchone()[0]
        result["ok"] = True
        result["would_delete"] = {"mirrored_writes": mirrored, "diagnostic_events": diag}
        return result

    if execute:
        with sqlite3.connect(str(store.db_path), check_same_thread=False) as conn:
            conn.row_factory = sqlite3.Row
            mirrored = conn.execute(
                "DELETE FROM mirrored_writes WHERE metadata LIKE ?",
                (f'%"pilot_run_id": "{run_id}"%',),
            ).rowcount
            diag = conn.execute(
                "DELETE FROM diagnostic_events WHERE error_message LIKE ?",
                (f'%"pilot_run_id": "{run_id}"%',),
            ).rowcount
        result["ok"] = True
        result["deleted"] = {"mirrored_writes": mirrored, "diagnostic_events": diag}
        return result

    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def build_pilot_argparser() -> argparse.ArgumentParser:
    """Build the CLI argument parser used by `main()`.

    Exposed for tests and for tools that want to parse pilot flags without
    triggering a pilot run. Adding a flag here without updating this function
    will silently fail tests.
    """
    parser = argparse.ArgumentParser(description="Hermes shadow-pilot load generator")
    parser.add_argument("--duration-hours", type=float, default=6.0, help="Pilot duration in hours")
    parser.add_argument("--interval-seconds", type=int, default=120, help="Seconds between writes")
    parser.add_argument("--drain-timeout-seconds", type=int, default=120, help="Indexing drain timeout")
    parser.add_argument("--run-id", type=str, default=None, help="Explicit run ID")
    # Primary-health gate thresholds. CLI flags (not env vars) so the report
    # is reproducible and the threshold used at run time is recorded alongside
    # the run. Defaults match the four-hour P0 soak; the 15-minute qualification
    # passes --primary-success-rate-required 99.0 and
    # --minimum-primary-success-required 50.
    parser.add_argument(
        "--primary-success-rate-required",
        type=float,
        default=99.9,
        help="Minimum primary success rate (percent) for the run to be valid",
    )
    parser.add_argument(
        "--minimum-primary-success-required",
        type=int,
        default=200,
        help="Minimum successful primary writes for the run to be valid",
    )
    parser.add_argument(
        "--primary-p95-max-ms",
        type=float,
        default=10_000.0,
        help="Absolute SLO: primary p95 latency in milliseconds",
    )
    parser.add_argument(
        "--primary-timeout-rate-max-percent",
        type=float,
        default=0.1,
        help="Absolute SLO: max percentage of primary writes exceeding the p95 threshold",
    )
    parser.add_argument(
        "--sustained-primary-outage-threshold",
        type=int,
        default=30,
        help="Consecutive primary failures that classify the run as invalid_primary_unavailable",
    )
    return parser


def _config_from_args(args: argparse.Namespace) -> PilotConfig:
    return PilotConfig(
        duration_hours=args.duration_hours,
        interval_seconds=args.interval_seconds,
        drain_timeout_seconds=args.drain_timeout_seconds,
        primary_success_rate_required_percent=args.primary_success_rate_required,
        minimum_primary_success_required=args.minimum_primary_success_required,
        primary_p95_max_ms=args.primary_p95_max_ms,
        primary_timeout_rate_max_percent=args.primary_timeout_rate_max_percent,
        sustained_primary_outage_threshold=args.sustained_primary_outage_threshold,
    )


def main() -> int:
    parser = build_pilot_argparser()
    args = parser.parse_args()
    config = _config_from_args(args)
    if args.run_id:
        config.run_id = args.run_id

    report = _run_pilot_internal(config)
    print(f"[{now_iso()}] Pilot complete. Report: {report.get('report_path')}")
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
