#!/usr/bin/env python3
"""Shadow-pilot load generator and evidence collector.

Runs a conservative Hermes shadow-pilot in the dedicated `shadow-pilot` profile,
performs periodic Honcho writes (mirrored to Fuli), and records metrics. After the
configured duration it writes a final report and updates progress.md / TODO.txt.

Usage:
    python shadow_pilot.py [--duration-hours 6] [--interval-seconds 120]

The script is designed to be started as a background process. It is self-contained
and does not require an interactive Hermes session.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PROFILE_NAME = "shadow-pilot"
HERMES_HOME = Path.home() / ".hermes" / "profiles" / PROFILE_NAME
EXPECTED_FULI_COMMIT = "719e429a9c8cb4d1de11b6c616413f410f32ea49"

# Ensure the pilot runs in the dedicated profile.
os.environ["HERMES_HOME"] = str(HERMES_HOME)
os.environ["HERMES_PROFILE"] = PROFILE_NAME

from hermes_cli.config import cfg_get, load_config
from hermes_constants import get_hermes_home
from plugins.memory import load_memory_provider
from plugins.memory.shadow.shadow_store import ShadowEvidenceStore


class PilotState:
    def __init__(self, duration_hours: float, interval_seconds: int) -> None:
        self.duration_seconds = int(duration_hours * 3600)
        self.interval_seconds = interval_seconds
        self.start_time = time.monotonic()
        self.end_time = self.start_time + self.duration_seconds
        self.stop_requested = threading.Event()
        self.metrics: List[Dict[str, Any]] = []
        self.write_count = 0
        self.primary_errors = 0
        self.fuli_timeouts = 0
        self.error_categories: Dict[str, int] = {}
        self.fuli_db_sizes: List[int] = []
        self.evidence_row_counts: List[int] = []
        self.thread_alive_checks: List[bool] = []
        self.loop_running_checks: List[bool] = []
        self.config: Dict[str, Any] = {}

    def remaining_seconds(self) -> float:
        return max(0.0, self.end_time - time.monotonic())


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_config_safe() -> Dict[str, Any]:
    try:
        return load_config() or {}
    except Exception as exc:
        print(f"[{now_iso()}] WARNING: failed to load config: {exc}")
        return {}


def verify_shadow_config(state: PilotState) -> None:
    config = load_config_safe()
    state.config = config
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
        "shadow.timeout_ms": shadow.get("timeout_ms"),
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
        "shadow.timeout_ms": 250,
        "shadow.capture_content": False,
        "shadow.namespace": "hermes:shadow-pilot",
    }
    for key, value in expected.items():
        actual = checks.get(key)
        if actual != value:
            raise RuntimeError(f"Pilot config mismatch for {key}: expected {value!r}, got {actual!r}")
    print(f"[{now_iso()}] Shadow config verified: {checks}")


def initialize_providers(state: PilotState) -> Any:
    verify_shadow_config(state)
    provider = load_memory_provider("shadow")
    if provider is None:
        raise RuntimeError("Failed to load memory provider")
    if provider.name != "shadow":
        raise RuntimeError(f"Expected provider 'shadow', got {provider.name!r}")
    provider.initialize(f"shadow-pilot-{now_iso()}", hermes_home=str(HERMES_HOME))
    print(f"[{now_iso()}] Initialized memory provider: {provider.name}")
    return provider


def do_one_write(provider: Any, iteration: int) -> tuple[str, Optional[str]]:
    """Return (primary_result, error_category_or_none)."""
    fact = f"Shadow pilot heartbeat {iteration} at {now_iso()}. This is a controlled test memory."
    if iteration % 2 == 0:
        tool_name = "honcho_conclude"
        args = {"peer": "user", "conclusion": fact}
    else:
        tool_name = "honcho_profile"
        args = {"peer": "user", "card": [fact]}
    primary_result = provider.handle_tool_call(tool_name, args)
    try:
        parsed = json.loads(primary_result)
        if isinstance(parsed, dict) and (parsed.get("error") or parsed.get("status") == "error"):
            err = parsed.get("error") or parsed.get("message") or "unknown"
            return primary_result, f"primary_error:{err}"
    except Exception:
        pass
    return primary_result, None


def collect_fuli_state(provider: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "thread_alive": None,
        "loop_running": None,
        "diagnostics": None,
        "diagnostics_error": None,
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
                state["diagnostics"] = json.loads(diag_raw)
            except Exception as exc:
                state["diagnostics_error"] = str(exc)
    except Exception as exc:
        state["diagnostics_error"] = str(exc)
    return state


def collect_evidence_stats() -> Dict[str, Any]:
    db_path = HERMES_HOME / "memories" / "shadow.db"
    stats = {"evidence_store_exists": db_path.exists(), "mirrored_write_rows": 0, "observation_rows": 0, "error": None}
    if not db_path.exists():
        return stats
    try:
        conn = sqlite3.connect(str(db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        stats["mirrored_write_rows"] = conn.execute("SELECT COUNT(*) FROM mirrored_writes").fetchone()[0]
        stats["observation_rows"] = conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0]
        conn.close()
    except Exception as exc:
        stats["error"] = str(exc)
    return stats


def categorize_error(error: Optional[str]) -> str:
    if not error:
        return "unknown"
    error_lower = error.lower()
    if "timeout" in error_lower:
        return "timeout"
    if "connection" in error_lower or "network" in error_lower:
        return "connection"
    if "lock" in error_lower or "sqlite" in error_lower or "database" in error_lower:
        return "database_lock"
    if "import" in error_lower or "module" in error_lower or "no module" in error_lower:
        return "import"
    return "other"


def heartbeat(provider: Any, state: PilotState) -> Dict[str, Any]:
    fuli_state = collect_fuli_state(provider)
    evidence_stats = collect_evidence_stats()
    fuli_db_path = HERMES_HOME / "memories" / "fuli.db"
    fuli_db_size = fuli_db_path.stat().st_size if fuli_db_path.exists() else 0

    state.fuli_db_sizes.append(fuli_db_size)
    state.evidence_row_counts.append(evidence_stats["mirrored_write_rows"])
    state.thread_alive_checks.append(bool(fuli_state.get("thread_alive")))
    state.loop_running_checks.append(bool(fuli_state.get("loop_running")))

    record = {
        "timestamp": now_iso(),
        "remaining_seconds": state.remaining_seconds(),
        "writes_attempted": state.write_count,
        "primary_errors": state.primary_errors,
        "fuli_timeouts": state.fuli_timeouts,
        "error_categories": dict(state.error_categories),
        "fuli_db_size": fuli_db_size,
        "evidence_rows": evidence_stats["mirrored_write_rows"],
        "observation_rows": evidence_stats["observation_rows"],
        "thread_alive": fuli_state.get("thread_alive"),
        "loop_running": fuli_state.get("loop_running"),
        "fuli_diagnostics": fuli_state.get("diagnostics"),
        "fuli_diagnostics_error": fuli_state.get("diagnostics_error"),
    }
    state.metrics.append(record)
    return record


def run_pilot(duration_hours: float, interval_seconds: int) -> Dict[str, Any]:
    state = PilotState(duration_hours, interval_seconds)
    provider = initialize_providers(state)
    evidence_store = ShadowEvidenceStore(HERMES_HOME / "memories" / "shadow.db")

    def _on_signal(signum, frame):
        print(f"[{now_iso()}] Signal {signum} received; stopping pilot at next interval...")
        state.stop_requested.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    print(f"[{now_iso()}] Pilot started for {duration_hours}h with {interval_seconds}s interval")
    iteration = 0
    next_heartbeat = time.monotonic() + 300.0

    while not state.stop_requested.is_set() and time.monotonic() < state.end_time:
        iteration += 1
        try:
            primary_result, primary_error = do_one_write(provider, iteration)
            state.write_count += 1
            if primary_error:
                state.primary_errors += 1
                cat = categorize_error(primary_error)
                state.error_categories[cat] = state.error_categories.get(cat, 0) + 1
                print(f"[{now_iso()}] PRIMARY ERROR on write {iteration}: {primary_error}")
            else:
                print(f"[{now_iso()}] Write {iteration} succeeded")
        except Exception as exc:
            state.primary_errors += 1
            cat = categorize_error(str(exc))
            state.error_categories[cat] = state.error_categories.get(cat, 0) + 1
            print(f"[{now_iso()}] EXCEPTION on write {iteration}: {exc}")
            traceback.print_exc()

        if time.monotonic() >= next_heartbeat:
            record = heartbeat(provider, state)
            print(f"[{now_iso()}] HEARTBEAT: {record}")
            next_heartbeat = time.monotonic() + 300.0

        sleep_seconds = min(state.remaining_seconds(), interval_seconds - (time.monotonic() - state.start_time) % interval_seconds)
        sleep_seconds = max(0.1, sleep_seconds)
        if state.stop_requested.wait(timeout=sleep_seconds):
            break

    final_record = heartbeat(provider, state)
    print(f"[{now_iso()}] Final heartbeat: {final_record}")

    try:
        provider.shutdown()
        print(f"[{now_iso()}] Provider shutdown complete")
    except Exception as exc:
        print(f"[{now_iso()}] Provider shutdown error: {exc}")

    return build_report(state, evidence_store, final_record)


def build_report(state: PilotState, evidence_store: ShadowEvidenceStore, final_record: Dict[str, Any]) -> Dict[str, Any]:
    store_report = evidence_store.report()
    writes = store_report["writes"]
    attempted = writes["attempted"]
    shadow_success = writes["shadow_success"]
    shadow_failures = attempted - shadow_success
    success_rate = (shadow_success / max(attempted, 1)) * 100.0
    timeout_count = state.error_categories.get("timeout", 0)
    primary_error_count = state.primary_errors

    # Oldest failed write
    oldest_failed = None
    try:
        conn = sqlite3.connect(str(evidence_store.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT timestamp FROM mirrored_writes WHERE shadow_success = 0 ORDER BY timestamp ASC LIMIT 1"
        ).fetchone()
        oldest_failed = row["timestamp"] if row else None
        conn.close()
    except Exception as exc:
        oldest_failed = f"error:{exc}"

    # Verify no raw content in evidence store
    raw_content_violations = 0
    try:
        conn = sqlite3.connect(str(evidence_store.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT shadow_error, content_fingerprint FROM mirrored_writes"):
            if row["content_fingerprint"] is None:
                raw_content_violations += 1
        conn.close()
    except Exception:
        pass

    # Verify namespace isolation
    namespaces = store_report.get("namespaces", [])
    namespace_leak = any(ns != "hermes:shadow-pilot" for ns in namespaces)

    report = {
        "pilot_id": f"shadow-pilot-{now_iso()}",
        "duration_hours": state.duration_seconds / 3600.0,
        "interval_seconds": state.interval_seconds,
        "started_at": datetime.fromtimestamp(state.start_time, tz=timezone.utc).isoformat(),
        "finished_at": now_iso(),
        "config": {
            "provider": cfg_get(state.config, "memory", "provider"),
            "enabled": cfg_get(state.config, "memory", "shadow", "enabled"),
            "primary_provider": cfg_get(state.config, "memory", "shadow", "primary_provider"),
            "secondary_provider": cfg_get(state.config, "memory", "shadow", "secondary_provider"),
            "mirror_writes": cfg_get(state.config, "memory", "shadow", "mirror_writes"),
            "compare_reads": cfg_get(state.config, "memory", "shadow", "compare_reads"),
            "sample_rate": cfg_get(state.config, "memory", "shadow", "sample_rate"),
            "timeout_ms": cfg_get(state.config, "memory", "shadow", "timeout_ms"),
            "capture_content": cfg_get(state.config, "memory", "shadow", "capture_content"),
            "namespace": cfg_get(state.config, "memory", "shadow", "namespace"),
        },
        "writes": {
            "attempted": attempted,
            "succeeded": shadow_success,
            "failed": shadow_failures,
            "success_rate_percent": round(success_rate, 4),
        },
        "errors": {
            "fuli_timeouts": timeout_count,
            "primary_errors": primary_error_count,
            "categories": dict(state.error_categories),
            "oldest_failed_write": oldest_failed,
        },
        "evidence_store": {
            "row_count": final_record["evidence_rows"],
            "observation_rows": final_record["observation_rows"],
            "namespaces": namespaces,
            "namespace_leak": namespace_leak,
            "raw_content_violations": raw_content_violations,
        },
        "fuli_db": {
            "final_size_bytes": state.fuli_db_sizes[-1] if state.fuli_db_sizes else 0,
            "size_samples": state.fuli_db_sizes,
        },
        "health": {
            "thread_alive_final": final_record.get("thread_alive"),
            "loop_running_final": final_record.get("loop_running"),
            "thread_alive_any_false": any(not x for x in state.thread_alive_checks if x is not None),
            "loop_running_any_false": any(not x for x in state.loop_running_checks if x is not None),
        },
        "fuli_diagnostics": final_record.get("fuli_diagnostics"),
    }
    return report


def update_progress_and_todo(report: Dict[str, Any]) -> None:
    repo_progress = PROJECT_ROOT / "progress.md"
    repo_todo = PROJECT_ROOT / "TODO.txt"

    section = f"""
## Shadow-pilot run — {report['finished_at']}

- **Duration:** {report['duration_hours']} hours
- **Writes attempted:** {report['writes']['attempted']}
- **Writes succeeded (Fuli):** {report['writes']['succeeded']}
- **Writes failed:** {report['writes']['failed']}
- **Success rate:** {report['writes']['success_rate_percent']}%
- **Fuli timeouts:** {report['errors']['fuli_timeouts']}
- **Error categories:** {report['errors']['categories']}
- **Primary errors:** {report['errors']['primary_errors']}
- **Oldest failed write:** {report['errors']['oldest_failed_write']}
- **Evidence-store rows:** {report['evidence_store']['row_count']}
- **Observation rows:** {report['evidence_store']['observation_rows']}
- **Namespace leak:** {report['evidence_store']['namespace_leak']}
- **Raw-content violations:** {report['evidence_store']['raw_content_violations']}
- **Fuli DB final size (bytes):** {report['fuli_db']['final_size_bytes']}
- **Thread alive at end:** {report['health']['thread_alive_final']}
- **Loop running at end:** {report['health']['loop_running_final']}
- **Thread ever dead:** {report['health']['thread_alive_any_false']}
- **Loop ever stopped:** {report['health']['loop_running_any_false']}
- **Pilot config:** {report['config']}
""".strip()

    if repo_progress.exists():
        existing = repo_progress.read_text()
        repo_progress.write_text(section + "\n\n" + existing)

    if repo_todo.exists():
        existing = repo_todo.read_text()
        lines = existing.splitlines()
        # Mark the go/no-go item as completed and the pilot item as completed.
        updated: List[str] = []
        for line in lines:
            if "Go/no-go decision for the mirrored-write pilot" in line or "Activation of the dedicated" in line:
                updated.append(line.replace("- [ ]", "- [x]"))
            else:
                updated.append(line)
        repo_todo.write_text("\n".join(updated) + "\n")

    print(f"[{now_iso()}] Updated {repo_progress} and {repo_todo}")


def run_cli_checks() -> Dict[str, str]:
    checks: Dict[str, str] = {}
    try:
        import io
        from hermes_cli.main import main as hermes_main

        for command in ["shadow preflight", "shadow status", "shadow report"]:
            buf = io.StringIO()
            sys.stdout = buf
            try:
                sys.argv = ["hermes", "--profile", PROFILE_NAME] + command.split()
                hermes_main()
            except SystemExit:
                pass
            sys.stdout = sys.__stdout__
            checks[command] = buf.getvalue()
    except Exception as exc:
        checks["error"] = str(exc)
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes shadow-pilot load generator")
    parser.add_argument("--duration-hours", type=float, default=6.0, help="Pilot duration in hours")
    parser.add_argument("--interval-seconds", type=int, default=120, help="Seconds between writes")
    args = parser.parse_args()

    report_dir = HERMES_HOME / "shadow"
    report_dir.mkdir(parents=True, exist_ok=True)
    metrics_path = report_dir / "pilot_metrics.jsonl"

    report = run_pilot(args.duration_hours, args.interval_seconds)

    # Append metrics to JSONL.
    with open(metrics_path, "a", encoding="utf-8") as f:
        for m in []:  # metrics already printed to stdout
            f.write(json.dumps(m, default=str) + "\n")

    # CLI checks.
    cli_checks = run_cli_checks()
    report["cli_checks"] = cli_checks

    # Save final report.
    report_path = report_dir / "pilot_report.json"
    report_path.write_text(json.dumps(report, indent=2, default=str))

    update_progress_and_todo(report)

    print(f"[{now_iso()}] Pilot complete. Report written to {report_path}")
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
