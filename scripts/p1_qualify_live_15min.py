"""Corrected 15-minute live 5% shadow pilot qualification driver.

This is the canonical, reproducible live qualification driver. It
exists to validate the v1/v2 corrections:

  * foreground overhead and primary invariance are inferred from the
    in/out telemetry contract (no second Honcho call per iteration);
  * evidence is run-scoped (unique run_id + fresh isolated comparisons.db);
  * warm-up is excluded from measurement (compare_reads=False, separate
    warm-up run_id);
  * the executor queue is backpressure-paced so it does not artificially
    flood;
  * the redacted disagreement export validates the privacy contract.

Usage:

    venv/bin/python scripts/p1_qualify_live_15min.py \\
        --duration-minutes 15 \\
        --qual-home /tmp/.... \\
        --output-dir ./reports/p1-qualified-.../

All database paths, log paths and report paths are deterministic from
the ``--qual-home`` and ``--run-id``. No /tmp scraping required.

Gates (every gate must pass for exit 0):

  - primary hash invariance 100%
  - enqueue overhead p50 < 2 ms, p95 < 10 ms, max < 25 ms
  - sampled >= 10
  - successful real Fuli comparisons >= 5
  - secondary success >= 80%
  - sampled == enqueued == started (after flush)
  - completed + timed_out + failed == persisted
  - run-scoped DB rows == persisted
  - queue drops == 0
  - persistence failures == 0
  - orphaned == 0
  - pending == 0 after flush
  - unexpected collisions == 0
  - duplicate idempotent == 0
  - only hermes:shadow-pilot namespace
  - content_captured == false everywhere
  - raw_query not in db or redacted export
  - SQLite integrity check = ok
  - SQLite quick check = ok
  - WAL checkpoint clean
  - persistence thread dead after shutdown
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import resource
import shutil
import signal
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

# Resolve project root so 'pilot' and 'plugins' packages are importable
# regardless of where the operator runs the script from.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Diverse safe queries covering profile / preference / project /
# episodic / exact / semantic / recent / contradiction. None contain
# secrets or private content. They are synthetic usage-pattern probes.
QUERY_SET = [
    # profile
    ("profile", "what is my name"),
    ("profile", "what language do I prefer"),
    ("profile", "what is my role"),
    ("profile", "what is my timezone"),
    ("profile", "what is my experience level"),
    # preference
    ("preference", "do I like short or long answers"),
    ("preference", "do I prefer markdown or plain text"),
    ("preference", "do I prefer code examples or prose"),
    ("preference", "do I prefer bullet lists or paragraphs"),
    ("preference", "do I prefer minimal or detailed explanations"),
    # project
    ("project", "what am I building"),
    ("project", "what stack am I using"),
    ("project", "what is the goal of my project"),
    ("project", "what is the next milestone"),
    ("project", "what is the deadline for my project"),
    # episodic
    ("episodic", "what did I work on yesterday"),
    ("episodic", "what was the last error I encountered"),
    ("episodic", "what was the last file I edited"),
    ("episodic", "what was the most recent thing I asked"),
    ("episodic", "what changed recently in my workflow"),
    # exact
    ("exact", "python"),
    ("exact", "rust"),
    ("exact", "kubernetes"),
    ("exact", "postgresql"),
    ("exact", "websocket"),
    # semantic
    ("semantic", "how do I improve code review"),
    ("semantic", "how do I handle timeouts gracefully"),
    ("semantic", "how do I test concurrent code"),
    ("semantic", "how do I design a fault-tolerant system"),
    ("semantic", "how do I structure error handling"),
    # recent
    ("recent", "what did I do today"),
    ("recent", "what was my most recent query"),
    ("recent", "what is fresh in my context"),
    # contradiction
    ("contradiction", "I said I prefer python but actually I prefer rust"),
    ("contradiction", "I said I work alone but I have a team"),
]


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def collect_resource_metrics() -> Dict[str, Any]:
    """Best-effort RSS / thread / FD snapshot via standard library only.

    Returns a dict with whatever metrics we can read on the current
    platform. Missing metrics return as None rather than raising.
    """
    out: Dict[str, Any] = {"timestamp": time.time()}
    try:
        # Python 3.11+ has os.getrusage with Linux ru_maxrss in KB;
        # macOS reports bytes.
        rusage = resource.getrusage(resource.RUSAGE_SELF)
        # macOS returns ru_maxrss in bytes; Linux returns KB.
        ru_maxrss = rusage.ru_maxrss
        out["rss_bytes"] = (
            ru_maxrss if sys.platform == "darwin" else ru_maxrss * 1024
        )
        out["user_cpu_seconds"] = rusage.ru_utime
        out["system_cpu_seconds"] = rusage.ru_stime
    except (OSError, ValueError):
        out["rss_bytes"] = None
        out["user_cpu_seconds"] = None
        out["system_cpu_seconds"] = None
    try:
        import threading as _threading
        out["thread_count"] = _threading.active_count()
    except Exception:
        out["thread_count"] = None
    try:
        # FD count via /proc/self/fd on Linux, /dev/fd on macOS.
        if os.path.isdir("/proc/self/fd"):
            out["fd_count"] = len(os.listdir("/proc/self/fd"))
        else:
            out["fd_count"] = len(os.listdir("/dev/fd"))
    except (OSError, PermissionError):
        out["fd_count"] = None
    return out


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    """Write JSON atomically: write to <path>.tmp, fsync, rename."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


class QualRun:
    """Self-contained live qualification runner.

    Builds an isolated QUAL_HOME + QUAL_RUN_ID, warms the providers,
    runs the workload, flushes and shuts down, and emits a JSON
    report. Mirrors the structure the operator docs/ops page expects.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.qual_home = Path(args.qual_home).resolve()
        self.output_dir = Path(args.output_dir).resolve()
        self.qual_home.mkdir(parents=True, exist_ok=True)
        (self.qual_home / "memories").mkdir(exist_ok=True)
        (self.qual_home / "fuli").mkdir(exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        # Checkpoint / stop / report paths.
        ckpt_dir = args.checkpoints_dir or str(self.output_dir / "checkpoints")
        self.checkpoints_dir = Path(ckpt_dir)
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self.report_json_path = (
            Path(args.report_json)
            if args.report_json
            else self.output_dir / "report.json"
        )
        self.stop_file = Path(args.stop_file) if args.stop_file else None
        self.checkpoint_seconds = max(0, args.checkpoint_minutes * 60)
        self.comparisons_db = self.qual_home / "memories" / "comparisons.db"
        self.fuli_db_src = Path("/Users/caiado/.hermes/profiles/shadow-pilot/memories/fuli.db")
        self.fuli_config_src = Path(
            "/Users/caiado/.hermes/profiles/shadow-pilot/fuli/config.json"
        )
        self.env_src = Path("/Users/caiado/.hermes/profiles/shadow-pilot/.env")
        self.run_id = args.run_id or self._generate_run_id()
        self.start_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.start_monotonic = time.monotonic()
        self.report: Dict[str, Any] = {
            "schema_version": 1,
            "tool": "p1_qualify_live_15min",
            "started_at": self.start_iso,
        }

    def _generate_run_id(self) -> str:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        suffix = uuid.uuid4().hex[:6]
        return f"p1-live5pct-qualify-{stamp}-{suffix}"

    # ------------------------------------------------------------------
    # Environment setup (must run BEFORE importing Hermes modules).
    # ------------------------------------------------------------------

    def isolate(self) -> None:
        """Build QUAL_HOME by copying the minimum required files.

        The live shadow-pilot profile is NEVER touched. Only the
        credentials file, the Fuli config, and the Fuli index DB are
        copied into the isolated home. The comparisons.db is created
        fresh on the shadow provider's first enqueue.
        """
        shutil.copy(self.env_src, self.qual_home / ".env")
        os.chmod(self.qual_home / ".env", 0o600)
        if self.fuli_config_src.exists():
            shutil.copy(self.fuli_config_src, self.qual_home / "fuli" / "config.json")
        # Copy the Fuli index DB so the qualification runs against
        # the same memories the live Fuli has indexed. This DB is
        # read-only by convention (the qualification writes its own
        # comparisons.db, not back to fuli.db).
        if self.fuli_db_src.exists():
            shutil.copy(self.fuli_db_src, self.qual_home / "memories" / "fuli.db")

        # Build a qualification-specific config.yaml so even an
        # operator who forgets to set env-var credentials on the live
        # profile can't accidentally leak a sample_rate change into
        # the live process. Pin everything explicitly.
        config_text = (
            "memory:\n"
            "  provider: shadow\n"
            "  shadow:\n"
            "    enabled: true\n"
            "    primary_provider: honcho\n"
            "    secondary_provider: fuli\n"
            "    mirror_writes: false\n"
            "    compare_reads: true\n"
            "    sample_rate: 0.05\n"
            "    sampling_seed: 0\n"
            "    comparison_budget_ms: 2000\n"
            "    comparison_max_workers: 1\n"
            "    comparison_max_queue_size: 128\n"
            "    write_timeout_ms: 10000\n"
            "    read_timeout_ms: 5000\n"
            "    capture_content: false\n"
            "    namespace: hermes:shadow-pilot\n"
            f"    comparison_run_id: {self.run_id}\n"
            "\n"
            "pilot:\n"
            "  duration_hours: 6.0\n"
            "  interval_seconds: 120\n"
        )
        (self.qual_home / "config.yaml").write_text(config_text)

        # Set the env BEFORE importing the shadow provider.
        os.environ["HERMES_HOME"] = str(self.qual_home)
        os.environ["HERMES_PROFILE"] = "shadow-pilot"

    # ------------------------------------------------------------------
    # The shadow provider is imported here lazily after env is set up.
    # ------------------------------------------------------------------

    def import_shadow(self) -> Any:
        from plugins.memory.shadow import ShadowMemoryProvider  # noqa: E402

        return ShadowMemoryProvider

    # ------------------------------------------------------------------
    # SQL helpers (always filtered by run_id).
    # ------------------------------------------------------------------

    def run_sql(self, query: str, params: tuple = ()) -> List[Dict[str, Any]]:
        if not self.comparisons_db.exists():
            return []
        con = sqlite3.connect(str(self.comparisons_db))
        try:
            cur = con.execute(query, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        finally:
            con.close()
        return rows

    # ------------------------------------------------------------------
    # Main qualification flow.
    # ------------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        self.isolate()
        ShadowMemoryProvider = self.import_shadow()

        # Build shadow provider (real Honcho + real Fuli).
        shadow = ShadowMemoryProvider()
        shadow.initialize(
            self.run_id,
            hermes_home=str(self.qual_home),
            comparison_run_id=self.run_id,
        )

        # ---- Warm-up (compare_reads=False, separate warm-up run_id) ----
        warmup_query = f"warmup-{uuid.uuid4().hex[:8]}"
        saved_compare_reads = shadow._compare_reads
        saved_run_id = shadow._comparison_run_id
        shadow._compare_reads = False
        shadow._comparison_run_id = f"{self.run_id}-warmup"
        t0 = time.monotonic()
        try:
            warmup_out = shadow.handle_tool_call(
                "honcho_search", {"query": warmup_query, "top_k": 3}
            )
            warmup_latency_ms = (time.monotonic() - t0) * 1000.0
            warmup_ok = True
            self.report["warmup"] = {
                "ok": True,
                "latency_ms": warmup_latency_ms,
                "payload_size": len(warmup_out),
                "warmup_run_id": shadow._comparison_run_id,
            }
        except Exception as e:
            warmup_ok = False
            self.report["warmup"] = {
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
            }
            return self._finalize(shadow, success=False)
        shadow._compare_reads = saved_compare_reads
        shadow._comparison_run_id = saved_run_id
        # Drain the warm-up so it doesn't pollute the measured run's executor.
        shadow.flush_comparisons(timeout_seconds=30.0)

        # Record pre-run row count for evidence boundary.
        before_count = len(
            self.run_sql(
                "SELECT comparison_id FROM comparisons WHERE run_id = ?",
                (self.run_id,),
            )
        )

        # ---- Controlled workload ----
        duration_seconds = max(60, self.args.duration_minutes * 60)
        deadline = time.monotonic() + duration_seconds

        # Per-iteration telemetry captured via the in/out contract.
        enqueue_overheads_ms: List[float] = []
        primary_latencies_ms: List[float] = []
        invariant_count = 0
        total_reads = 0
        sampled_count = 0
        invariant_failures: List[str] = []

        # Pause / stop wiring.
        # - signal-driven stop event: SIGINT/SIGTERM → cooperative stop.
        # - stop_file watcher: presence of self.stop_file → cooperative stop.
        # - checkpoint loop: every checkpoint_seconds, write JSON.
        stop_requested = {"value": False, "reason": "", "gate": ""}

        def _request_stop(reason: str, gate: str) -> None:
            stop_requested["value"] = True
            stop_requested["reason"] = reason
            stop_requested["gate"] = gate

        def _sig_stop(signum, frame):  # noqa: ARG001
            _request_stop(f"signal {signum}", "")

        try:
            signal.signal(signal.SIGINT, _sig_stop)
            signal.signal(signal.SIGTERM, _sig_stop)
        except (ValueError, OSError):
            # SIGTERM may not be installable from non-main threads; ignore.
            pass

        next_checkpoint_at = (
            time.monotonic() + self.checkpoint_seconds
            if self.checkpoint_seconds > 0
            else None
        )
        i = 0
        while time.monotonic() < deadline and not stop_requested["value"]:
            # Stop sentinel: if stop_file exists, request graceful stop.
            if self.stop_file and self.stop_file.exists():
                _request_stop(
                    f"stop file present: {self.stop_file}",
                    "explicit_stop_file",
                )
                break

            qtype, query = QUERY_SET[i % len(QUERY_SET)]
            i += 1
            total_reads += 1
            telemetry: Dict[str, Any] = {}
            t0_s = time.monotonic()
            try:
                _ = shadow.handle_tool_call(
                    "honcho_search",
                    {"query": query, "top_k": 3},
                    telemetry=telemetry,
                )
            except Exception as e:
                self.report["error"] = f"read {total_reads}: shadow raised {type(e).__name__}: {e}"
                continue
            primary_latencies_ms.append(telemetry.get("primary_latency_ms", 0.0))

            # Invariance: primary_result_sha256 == returned_result_sha256.
            prim_hash = telemetry.get("primary_result_sha256", "")
            ret_hash = telemetry.get("returned_result_sha256", "")
            if prim_hash == ret_hash and prim_hash:
                invariant_count += 1
            else:
                invariant_failures.append(
                    f"i={total_reads} prim={prim_hash[:12]} ret={ret_hash[:12]}"
                )

            if (
                telemetry.get("sampled")
                and "enqueue_started_at_ms" in telemetry
                and "enqueue_completed_at_ms" in telemetry
            ):
                sampled_count += 1
                overhead = (
                    telemetry["enqueue_completed_at_ms"]
                    - telemetry["enqueue_started_at_ms"]
                )
                enqueue_overheads_ms.append(overhead)

            # Backpressure so the executor queue is not artificially
            # flooded. At 5% sample rate and a worker that processes
            # ~7-8 comparisons/min, ~16 reads/min keeps queue depth
            # bounded while staying well above the >=10 sampled target
            # for any 15-minute window.
            time.sleep(3.7)

            # Periodic checkpoint + auto-pause evaluation.
            if (
                next_checkpoint_at is not None
                and time.monotonic() >= next_checkpoint_at
            ):
                auto_pause_gate = self._write_checkpoint(
                    shadow,
                    enqueue_overheads_ms,
                    primary_latencies_ms,
                    total_reads,
                    sampled_count,
                    invariant_count,
                    invariant_failures,
                    stop_requested["reason"],
                )
                if auto_pause_gate:
                    _request_stop(
                        f"auto-pause gate triggered: {auto_pause_gate}",
                        auto_pause_gate,
                    )
                    break
                next_checkpoint_at = (
                    time.monotonic() + self.checkpoint_seconds
                )

        workload_elapsed = time.monotonic() - self.start_monotonic
        self.report["stop_reason"] = stop_requested["reason"]
        self.report["stop_gate"] = stop_requested["gate"]

        # ---- Bounded flush ----
        flush_start = time.monotonic()
        flush_result = shadow.flush_comparisons(timeout_seconds=240.0)
        flush_elapsed = time.monotonic() - flush_start

        # ---- Executor accounting ----
        acc = shadow.executor_accounting()
        self.report["executor_accounting"] = acc
        self.report["is_balanced"] = shadow.executor_is_balanced()

        # ---- Run-scoped rows ----
        # IMPORTANT: content_captured MUST be in the SELECT so the
        # privacy gate below can evaluate it. The earlier driver
        # silently dropped this column and the False branch tripped
        # against None, which is the parent of the v4 NO-GO.
        all_rows = self.run_sql(
            "SELECT comparison_id, run_id, namespace, content_captured, "
            "secondary_status, secondary_error_category, secondary_latency_ms "
            "FROM comparisons WHERE run_id = ? ORDER BY timestamp",
            (self.run_id,),
        )

        # ---- Foreground aggregates ----
        secondary_latencies = sorted(
            [r.get("secondary_latency_ms") or 0 for r in all_rows]
        )

        # ---- Privacy / namespace ----
        all_namespaces = set(r.get("namespace") for r in all_rows)
        serialized = json.dumps([list(r.values()) for r in all_rows], default=str)
        raw_query_hits_db = sum(1 for _, q in QUERY_SET if q in serialized)
        all_content_captured_false = (
            all(r.get("content_captured") == 0 for r in all_rows)
            if all_rows
            else False
        )

        # ---- SQLite / WAL integrity ----
        con = sqlite3.connect(str(self.comparisons_db))
        ic = con.execute("PRAGMA integrity_check").fetchall()
        qc = con.execute("PRAGMA quick_check").fetchall()
        wal = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        con.close()

        # ---- Clean shutdown ----
        shadow.shutdown()
        alive = (
            shadow._executor._persistence_thread.is_alive()
            if shadow._executor and shadow._executor._persistence_thread
            else False
        )

        # ---- Redacted export ----
        redacted_path = self.output_dir / "redacted_disagreements.json"
        export_ok, export_count, export_count_run = self._run_redacted_export(
            redacted_path, shadow, acc["comparison_jobs_persisted"]
        )
        raw_query_hits_export = 0
        fuli_marker_export = False
        if export_ok and redacted_path.exists():
            try:
                with open(redacted_path) as f:
                    exp = json.load(f)
                exp_serialized = json.dumps(exp, default=str)
                raw_query_hits_export = sum(
                    1 for _, q in QUERY_SET if q in exp_serialized
                )
                fuli_marker_export = "fuli_marker_leaked" in exp_serialized.lower()
                # Only count rows for our run_id when verifying.
                export_count_run = sum(
                    1 for c in exp.get("comparisons", [])
                    if c.get("run_id") == self.run_id
                )
            except Exception:
                pass

        secondary_successful = sum(
            1 for r in all_rows if r.get("secondary_status") == "success"
        )
        secondary_rate = secondary_successful / max(1, len(all_rows))

        fg_p50 = percentile(enqueue_overheads_ms, 50) if enqueue_overheads_ms else None
        fg_p95 = percentile(enqueue_overheads_ms, 95) if enqueue_overheads_ms else None
        fg_max = max(enqueue_overheads_ms) if enqueue_overheads_ms else None

        sampled = acc["comparison_jobs_sampled"]
        enqueued = acc["comparison_jobs_enqueued"]
        started = acc["comparison_jobs_started"]
        completed = acc["comparison_jobs_completed"]
        timed_out = acc["comparison_jobs_timed_out"]
        failed = acc["comparison_jobs_failed"]
        persisted = acc["comparison_jobs_persisted"]

        checks: Dict[str, bool] = {
            "primary_hash_invariance_100": invariant_count == total_reads,
            "enqueue_overhead_p50_under_2ms": (fg_p50 is not None and fg_p50 < 2.0),
            "enqueue_overhead_p95_under_10ms": (fg_p95 is not None and fg_p95 < 10.0),
            "enqueue_overhead_max_under_25ms": (fg_max is not None and fg_max < 25.0),
            "sampled_at_least_10": sampled >= 10,
            "successful_real_fuli_at_least_5": secondary_successful >= 5,
            "secondary_success_at_least_80pct": secondary_rate >= 0.80,
            "sampled_equals_enqueued": sampled == enqueued,
            "enqueued_equals_started_after_flush": enqueued == started,
            "completed_plus_timed_out_plus_failed_equals_persisted": (
                completed + timed_out + failed == persisted
            ),
            "run_scoped_db_rows_equals_persisted": len(all_rows) == persisted,
            "queue_drops_zero": acc["comparison_jobs_dropped_queue_full"] == 0,
            "persistence_failures_zero": acc["persistence_failed"] == 0,
            "orphaned_zero": acc["comparison_jobs_orphaned"] == 0,
            "pending_zero_after_flush": acc["comparison_jobs_pending"] == 0,
            "unexpected_collisions_zero": acc["unexpected_collision"] == 0,
            "duplicate_idempotent_zero": acc["duplicate_idempotent"] == 0,
            "is_balanced": self.report["is_balanced"],
            "namespace_isolated": all_namespaces == {"hermes:shadow-pilot"},
            "content_captured_false_everywhere": all_content_captured_false,
            "raw_query_not_in_db": raw_query_hits_db == 0,
            "redacted_export_ok": export_ok,
            "raw_query_not_in_export": raw_query_hits_export == 0,
            "no_fuli_marker_in_export": not fuli_marker_export,
            "export_filtered_to_run_id": (
                export_count_run == persisted if export_ok else False
            ),
            "persistence_thread_dead_after_shutdown": not alive,
            "integrity_check_ok": ic == [("ok",)],
            "quick_check_ok": qc == [("ok",)],
            "wal_checkpoint_clean": wal == [(0, 0, 0)],
        }

        self.report.update(
            {
                "warmup_ok": warmup_ok,
                "pre_run_row_count": before_count,
                "workload_elapsed_seconds": workload_elapsed,
                "total_reads": total_reads,
                "sampled_count_telemetry": sampled_count,
                "invariant_count": invariant_count,
                "invariant_failures_count": len(invariant_failures),
                "invariant_failures_sample": invariant_failures[:5],
                "flush_elapsed_seconds": flush_elapsed,
                "flush_flushed": flush_result.get("flushed"),
                "run_scoped_row_count": len(all_rows),
                "status_breakdown": {
                    "success": secondary_successful,
                    "failed": len(all_rows) - secondary_successful,
                    "secondary_success_rate": secondary_rate,
                },
                "secondary_latency_ms": {
                    "p50": percentile(secondary_latencies, 50),
                    "p95": percentile(secondary_latencies, 95),
                    "max": max(secondary_latencies) if secondary_latencies else 0.0,
                },
                "enqueue_overhead_ms": {
                    "p50": fg_p50,
                    "p95": fg_p95,
                    "max": fg_max,
                    "n": len(enqueue_overheads_ms),
                },
                "primary_latency_ms": {
                    "p50": percentile(primary_latencies_ms, 50),
                    "p95": percentile(primary_latencies_ms, 95),
                    "max": max(primary_latencies_ms) if primary_latencies_ms else 0.0,
                },
                "namespaces": sorted(all_namespaces),
                "all_content_captured_false": all_content_captured_false,
                "raw_query_hits_db": raw_query_hits_db,
                "raw_query_hits_export": raw_query_hits_export,
                "fuli_marker_in_export": fuli_marker_export,
                "integrity_check": ic,
                "quick_check": qc,
                "wal_checkpoint": wal,
                "persistence_thread_alive_after_shutdown": alive,
                "redacted_export_path": str(redacted_path),
                "redacted_export_count": export_count,
                "redacted_export_count_run_scoped": export_count_run,
                "accounting_identities": {
                    "sampled_equals_enqueued": f"{sampled}=={enqueued}",
                    "enqueued_equals_started": f"{enqueued}=={started}",
                    "completed_plus_timed_out_plus_failed_equals_persisted": (
                        f"{completed}+{timed_out}+{failed}=={persisted}"
                    ),
                    "run_scoped_db_rows_equals_persisted": f"{len(all_rows)}=={persisted}",
                },
                "checks": checks,
            }
        )

        return self._finalize(shadow, success=all(checks.values()))

    # ------------------------------------------------------------------
    # Checkpoint + auto-pause
    # ------------------------------------------------------------------

    def _write_checkpoint(
        self,
        shadow: Any,
        enqueue_overheads_ms: List[float],
        primary_latencies_ms: List[float],
        total_reads: int,
        sampled_count: int,
        invariant_count: int,
        invariant_failures: List[str],
        current_stop_reason: str,
    ) -> Optional[str]:
        """Write a checkpoint JSON and evaluate hard-pause gates.

        Returns the gate name that triggered an auto-pause (or None).
        The checkpoint is written atomically via tmp+rename and includes
        executor accounting, SQLite integrity, and resource metrics.
        """
        # Explicit flush before each checkpoint to drain the executor.
        try:
            shadow.flush_comparisons(timeout_seconds=30.0)
        except Exception:
            pass

        acc = shadow.executor_accounting()
        balanced = shadow.executor_is_balanced()

        # Run-scoped rows for SQLite integrity, namespace, and
        # content_captured checks.
        all_rows = self.run_sql(
            "SELECT comparison_id, run_id, namespace, content_captured, "
            "secondary_status, secondary_error_category, secondary_latency_ms "
            "FROM comparisons WHERE run_id = ?",
            (self.run_id,),
        )

        secondary_latencies_sorted = sorted(
            [r.get("secondary_latency_ms") or 0 for r in all_rows]
        )
        all_namespaces = set(r.get("namespace") for r in all_rows)
        all_content_captured_false = (
            all(r.get("content_captured") == 0 for r in all_rows)
            if all_rows
            else False
        )

        secondary_successful = sum(
            1 for r in all_rows if r.get("secondary_status") == "success"
        )
        secondary_total = len(all_rows)
        success_rate = (
            secondary_successful / secondary_total if secondary_total else 1.0
        )

        # SQLite / WAL
        con = sqlite3.connect(str(self.comparisons_db))
        ic = con.execute("PRAGMA integrity_check").fetchall()
        qc = con.execute("PRAGMA quick_check").fetchall()
        wal = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        con.close()

        resources = collect_resource_metrics()

        primary_mutations = total_reads - invariant_count

        # Roll-up
        ckpt = {
            "schema_version": 1,
            "kind": "checkpoint",
            "run_id": self.run_id,
            "qual_home": str(self.qual_home),
            "comparisons_db": str(self.comparisons_db),
            "checkpoint_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "elapsed_seconds": time.monotonic() - self.start_monotonic,
            "total_reads": total_reads,
            "sampled_reads": sampled_count,
            "persisted_rows_run_scoped": secondary_total,
            "secondary": {
                "success_count": secondary_successful,
                "non_success_count": secondary_total - secondary_successful,
                "secondary_success_rate": success_rate,
                "p50_ms": percentile(secondary_latencies_sorted, 50),
                "p95_ms": percentile(secondary_latencies_sorted, 95),
                "max_ms": (
                    max(secondary_latencies_sorted)
                    if secondary_latencies_sorted
                    else 0.0
                ),
            },
            "enqueue_overhead_ms": {
                "p50": percentile(enqueue_overheads_ms, 50),
                "p95": percentile(enqueue_overheads_ms, 95),
                "max": (
                    max(enqueue_overheads_ms)
                    if enqueue_overheads_ms
                    else 0.0
                ),
                "n": len(enqueue_overheads_ms),
            },
            "primary_latency_ms": {
                "p50": percentile(primary_latencies_ms, 50),
                "p95": percentile(primary_latencies_ms, 95),
                "max": (
                    max(primary_latencies_ms)
                    if primary_latencies_ms
                    else 0.0
                ),
            },
            "executor_accounting": acc,
            "is_balanced": balanced,
            "queue": {
                "dropped_queue_full": acc["comparison_jobs_dropped_queue_full"],
                "depth": acc.get("queue_depth", 0),
                "high_watermark": acc.get("queue_high_watermark", 0),
                "persistence_depth": acc.get("persistence_queue_depth", 0),
            },
            "persistence": {
                "failures": acc["persistence_failed"],
                "started": acc.get("persistence_started", 0),
            },
            "orphans": acc["comparison_jobs_orphaned"],
            "collisions": acc["unexpected_collision"],
            "pending": acc["comparison_jobs_pending"],
            "primary_hash_mutations": primary_mutations,
            "invariant_failures_sample": invariant_failures[-5:],
            "privacy": {
                "namespaces": sorted(all_namespaces),
                "all_content_captured_false": all_content_captured_false,
            },
            "integrity_check": ic,
            "quick_check": qc,
            "wal_checkpoint": wal,
            "resources": resources,
            "running_health": (
                "degraded" if 0.80 <= success_rate < 0.90 else "healthy"
            ),
            "current_stop_reason": current_stop_reason,
        }

        # ---- Hard-pause gates ----
        auto_pause_gate: Optional[str] = None
        if primary_mutations > 0:
            auto_pause_gate = "primary_output_mutation"
        elif all_namespaces and all_namespaces != {"hermes:shadow-pilot"}:
            auto_pause_gate = "namespace_violation"
        elif all_rows and not all_content_captured_false:
            auto_pause_gate = "raw_content_violation"
        elif acc["unexpected_collision"] > 0:
            auto_pause_gate = "unexpected_collision"
        elif acc["persistence_failed"] > 0:
            auto_pause_gate = "persistence_failure"
        elif acc["comparison_jobs_dropped_queue_full"] > 0:
            auto_pause_gate = "queue_drops"
        elif acc["comparison_jobs_orphaned"] > 0:
            auto_pause_gate = "orphaned_worker"
        elif ic != [("ok",)] or qc != [("ok",)]:
            auto_pause_gate = "sqlite_integrity_failure"
        elif secondary_total >= 20 and success_rate < 0.80:
            auto_pause_gate = "fuli_secondary_success_below_80pct"

        ckpt["auto_pause_gate"] = auto_pause_gate
        ckpt["degraded"] = (
            auto_pause_gate is None
            and 0.80 <= success_rate < 0.90
        )

        # Atomic write.
        ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        ckpt_path = self.checkpoints_dir / f"checkpoint-{ts}.json"
        try:
            atomic_write_json(ckpt_path, ckpt)
        except Exception as e:
            # Failing to write a checkpoint is bad but must not kill the run.
            print(f"checkpoint write failed: {type(e).__name__}: {e}")

        # Mirror to <output_dir>/report.json so the operator can
        # `cat` one file at any moment.
        try:
            atomic_write_json(self.report_json_path, ckpt)
        except Exception:
            pass

        return auto_pause_gate

    def _run_redacted_export(
        self, path: Path, shadow: Any, expected_persisted: int
    ) -> tuple[bool, int, int]:
        """Run hermes shadow disagreements export --redacted.

        Returns (export_ok, export_count, export_count_run_scoped).
        """
        import subprocess

        env = os.environ.copy()
        env["HERMES_HOME"] = str(self.qual_home)
        env["HERMES_PROFILE"] = "shadow-pilot"
        hermes_bin = str(REPO_ROOT / "venv" / "bin" / "hermes")
        try:
            cp = subprocess.run(
                [hermes_bin, "shadow", "disagreements", "export",
                 "--redacted", "--output", str(path)],
                capture_output=True,
                env=env,
                cwd=str(REPO_ROOT),
                timeout=60,
            )
            ok = cp.returncode == 0
            count = 0
            count_run = 0
            if path.exists():
                try:
                    with open(path) as f:
                        data = json.load(f)
                    count = int(data.get("count", 0)) if isinstance(data, dict) else 0
                    count_run = sum(
                        1 for c in data.get("comparisons", [])
                        if c.get("run_id") == self.run_id
                    ) if isinstance(data, dict) else 0
                except Exception:
                    pass
            return ok, count, count_run
        except Exception as e:
            self.report["redacted_export_error"] = f"{type(e).__name__}: {e}"
            return False, 0, 0

    def _finalize(self, shadow: Any, success: bool) -> Dict[str, Any]:
        end_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.report["ended_at"] = end_iso
        self.report["run_id"] = self.run_id
        self.report["qual_home"] = str(self.qual_home)
        self.report["comparisons_db"] = str(self.comparisons_db)
        self.report["output_dir"] = str(self.output_dir)
        self.report["duration_minutes"] = self.args.duration_minutes
        self.report["decision"] = "GO" if success else "NO-GO"
        self.report["success"] = success

        # Emit JSON report. If we wrote checkpoints, the final
        # report.json mirrors the latest checkpoint envelope.
        self.report_path = str(self.report_json_path)
        if not self.checkpoints_dir or not any(
            (self.checkpoints_dir / f"checkpoint-{ts}.json").exists()
            for ts in [
                time.strftime(
                    "%Y%m%dT%H%M%S",
                    time.gmtime(),
                )
            ]
        ):
            # Fall back to writing the final report under
            # report_json_path, even when no checkpoint fired
            # (e.g. duration too short for any checkpoint).
            try:
                atomic_write_json(self.report_json_path, self.report)
            except Exception:
                pass

        # Emit console summary.
        print()
        print("=" * 78)
        print(f"QUALIFICATION RUN_ID: {self.run_id}")
        print(f"QUAL_HOME: {self.qual_home}")
        print(f"COMPARISONS_DB: {self.comparisons_db}")
        print(f"OUTPUT_DIR: {self.output_dir}")
        print(f"DURATION_MINUTES: {self.args.duration_minutes}")
        print(f"DECISION: {self.report['decision']}")
        print(f"Report JSON: {self.report_path}")
        print(f"Redacted export: {self.report.get('redacted_export_path')}")
        print("=" * 78)
        for k, v in self.report.get("checks", {}).items():
            print(f"  [{'PASS' if v else 'FAIL'}] {k}")
        print()
        print(f"sampled={self.report['executor_accounting'].get('comparison_jobs_sampled')} "
              f"enqueued={self.report['executor_accounting'].get('comparison_jobs_enqueued')} "
              f"started={self.report['executor_accounting'].get('comparison_jobs_started')} "
              f"persisted={self.report['executor_accounting'].get('comparison_jobs_persisted')} "
              f"completed={self.report['executor_accounting'].get('comparison_jobs_completed')} "
              f"timed_out={self.report['executor_accounting'].get('comparison_jobs_timed_out')} "
              f"failed={self.report['executor_accounting'].get('comparison_jobs_failed')} "
              f"is_balanced={self.report['is_balanced']}")
        return self.report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a controlled live shadow pilot qualification or soak.",
    )
    parser.add_argument(
        "--duration-minutes",
        type=int,
        default=15,
        help="Workload duration in minutes (>= 1). Default: 15.",
    )
    parser.add_argument(
        "--run-id",
        type=str,
        default="",
        help="Optional explicit run_id. Generated when omitted.",
    )
    parser.add_argument(
        "--qual-home",
        type=str,
        default="/tmp/hermes-p1-qualify",
        help="Isolated QUAL_HOME parent directory. "
             "A unique subdirectory is created under it.",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="",
        help="Directory where reports and the redacted export land. "
             "Defaults to reports/p1-<run_id>/ under the repo.",
    )
    parser.add_argument(
        "--checkpoint-minutes",
        type=int,
        default=0,
        help="If > 0, write a checkpoint every N minutes (default 0 = disabled).",
    )
    parser.add_argument(
        "--pid-file",
        type=str,
        default="",
        help="Optional PID file path. Written at start, removed at clean exit.",
    )
    parser.add_argument(
        "--stop-file",
        type=str,
        default="",
        help="Optional STOP sentinel. When the file exists, the driver "
             "writes a final checkpoint and gracefully stops at the next "
             "check.",
    )
    parser.add_argument(
        "--checkpoints-dir",
        type=str,
        default="",
        help="Directory where checkpoint JSON files land. Defaults to "
             "<output-dir>/checkpoints/.",
    )
    parser.add_argument(
        "--report-json",
        type=str,
        default="",
        help="Explicit path for the final machine-readable JSON report.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.duration_minutes < 1:
        print("--duration-minutes must be >= 1", file=sys.stderr)
        return 2
    if not args.output_dir:
        stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        args.output_dir = str(
            REPO_ROOT / "reports" / f"p1-live-qualify-{stamp}"
        )
    if not args.run_id:
        args.run_id = ""
    qual_home_parent = Path(args.qual_home)
    qual_home_parent.mkdir(parents=True, exist_ok=True)
    suffix = uuid.uuid4().hex[:8]
    args.qual_home = str(qual_home_parent / f"run-{suffix}")

    # Write PID file (operator convenience; not part of the evidence).
    if args.pid_file:
        Path(args.pid_file).write_text(str(os.getpid()))

    run = QualRun(args)
    try:
        report = run.run()
    finally:
        if args.pid_file and Path(args.pid_file).exists():
            try:
                Path(args.pid_file).unlink()
            except OSError:
                pass

    return 0 if report.get("success") else 2


if __name__ == "__main__":
    sys.exit(main())
