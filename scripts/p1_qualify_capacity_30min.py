"""30-minute real-provider capacity qualification driver.

Goal: prove the admission-capacity fix holds against real Honcho +
real Fuli at pinned commit. Cross jobs 129, 256, 300 with at least
300 accepted comparisons in 30 minutes.

Unlike the 15-minute qualification, this driver is not paced by
3.7s sleep between shadow.handle_tool_call invocations. Instead,
it continuously drives the shadow's executor enqueue path with
real primary + secondary calls, pacing only enough to keep the
worker from saturating.

Configuration (matches the brief):
  - duration_minutes: 30
  - sample_rate: 1.0 (every iteration is admitted)
  - max_queue_size: 128
  - max_workers: 1
  - comparison_budget_ms: 2000
  - capture_content: false
  - mirror_writes: false (controlled run)
  - namespace: hermes:shadow-pilot
  - 5-minute checkpoint interval

Output: isolated QUAL_HOME, fresh comparisons.db, dedup'd report
saved under reports/<run_id>/report.json.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import sqlite3
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

QUERY_SET: List[str] = [
    "qual-query " + str(i) for i in range(64)
]

def sha256_hex(s: str) -> str:
    import hashlib as _h
    return _h.sha256(s.encode("utf-8", "replace")).hexdigest()


def percentile(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    k = max(0, min(len(s)-1, int(round((p/100.0)*(len(s)-1)))))
    return s[k]


def setup_isolated_home(out_dir: Path, parent: Path, run_id: str) -> Path:
    qual_home = parent / f"run-{uuid.uuid4().hex[:8]}"
    qual_home.mkdir(parents=True, exist_ok=True)
    (qual_home / "memories").mkdir(exist_ok=True)
    (qual_home / "fuli").mkdir(exist_ok=True)
    shutil.copy(
        Path("/Users/caiado/.hermes/profiles/shadow-pilot/.env"),
        qual_home / ".env",
    )
    os.chmod(qual_home / ".env", 0o600)
    shutil.copy(
        Path("/Users/caiado/.hermes/profiles/shadow-pilot/fuli/config.json"),
        qual_home / "fuli" / "config.json",
    )
    if (Path("/Users/caiado/.hermes/profiles/shadow-pilot/memories/fuli.db")).exists():
        shutil.copy(
            Path("/Users/caiado/.hermes/profiles/shadow-pilot/memories/fuli.db"),
            qual_home / "memories" / "fuli.db",
        )
    config = (
        "memory:\n"
        "  provider: shadow\n"
        "  shadow:\n"
        "    enabled: true\n"
        "    primary_provider: honcho\n"
        "    secondary_provider: fuli\n"
        "    mirror_writes: false\n"
        "    compare_reads: true\n"
        "    sample_rate: 1.0\n"
        "    sampling_seed: 0\n"
        "    comparison_budget_ms: 2000\n"
        "    comparison_max_workers: 1\n"
        "    comparison_max_queue_size: 128\n"
        "    write_timeout_ms: 10000\n"
        "    read_timeout_ms: 5000\n"
        "    capture_content: false\n"
        "    namespace: hermes:shadow-pilot\n"
        f"    comparison_run_id: {run_id}\n"
        "\npilot:\n"
        "  duration_hours: 6.0\n"
        "  interval_seconds: 120\n"
    )
    (qual_home / "config.yaml").write_text(config)
    return qual_home


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration-minutes", type=int, default=30)
    parser.add_argument("--run-id", type=str, default="")
    parser.add_argument("--output-dir", type=str, default="")
    parser.add_argument("--checkpoint-minutes", type=int, default=5)
    parser.add_argument("--pid-file", type=str, default="")
    parser.add_argument("--stop-file", type=str, default="")
    args = parser.parse_args()

    if not args.run_id:
        args.run_id = f"p1-capacity-qual-30min-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    if not args.output_dir:
        args.output_dir = str(REPO_ROOT / "reports" / args.run_id)

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "checkpoints").mkdir(exist_ok=True)
    qual_home = setup_isolated_home(
        out_dir,
        Path("/tmp/hermes-p1-qualify"),
        args.run_id,
    )
    os.environ["HERMES_HOME"] = str(qual_home)
    os.environ["HERMES_PROFILE"] = "shadow-pilot"

    if args.pid_file:
        Path(args.pid_file).write_text(str(os.getpid()))

    sys.path.insert(0, str(REPO_ROOT))
    from plugins.memory.shadow import ShadowMemoryProvider

    shadow = ShadowMemoryProvider()
    shadow.initialize(args.run_id, hermes_home=str(qual_home),
                     comparison_run_id=args.run_id)

    # Warm-up
    shadow._compare_reads = False
    shadow._comparison_run_id = f"{args.run_id}-warmup"
    t0 = time.monotonic()
    try:
        _ = shadow.handle_tool_call("honcho_search", {"query": "warmup", "top_k": 3})
        warmup_ms = (time.monotonic() - t0) * 1000
        warmup_ok = True
    except Exception as e:
        warmup_ok = False
        warmup_ms = 0
        print(f"warmup failed: {type(e).__name__}: {e}")
    shadow.flush_comparisons(timeout_seconds=30.0)
    shadow._compare_reads = True
    shadow._comparison_run_id = args.run_id

    comparisons_db = qual_home / "memories" / "comparisons.db"

    def checkpoint(reason: str = ""):
        shadow.flush_comparisons(timeout_seconds=30.0)
        acc = shadow.executor_accounting()
        balanced = shadow.executor_is_balanced()
        all_rows = []
        if comparisons_db.exists():
            con = sqlite3.connect(str(comparisons_db))
            cur = con.execute(
                "SELECT comparison_id, run_id, namespace, content_captured, "
                "secondary_status, secondary_error_category, secondary_latency_ms "
                "FROM comparisons WHERE run_id = ?",
                (args.run_id,),
            )
            cols = [d[0] for d in cur.description]
            all_rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            con.close()
        ok = sum(1 for r in all_rows if r.get("secondary_status") == "success")
        rate = ok / max(1, len(all_rows))
        secondary_latencies = sorted(
            [r.get("secondary_latency_ms") or 0 for r in all_rows]
        )
        ack_path = out_dir / "report.json"
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        cp_path = out_dir / "checkpoints" / f"checkpoint-{time.strftime('%Y%m%dT%H%M%S', time.gmtime())}.json"
        ckpt = {
            "schema_version": 1,
            "kind": "checkpoint",
            "run_id": args.run_id,
            "qual_home": str(qual_home),
            "comparisons_db": str(comparisons_db),
            "checkpoint_at": ts,
            "elapsed_seconds": time.monotonic() - start,
            "reason": reason,
            "executor_accounting": acc,
            "is_balanced": balanced,
            "run_scoped_rows": len(all_rows),
            "secondary_success_rate": rate,
            "secondary_p50_ms": percentile(secondary_latencies, 50),
            "secondary_p95_ms": percentile(secondary_latencies, 95),
            "secondary_max_ms": max(secondary_latencies) if secondary_latencies else 0.0,
            "all_content_captured_false": (
                all(r.get("content_captured") == 0 for r in all_rows)
                if all_rows else False
            ),
        }
        # Integrity checks
        con = sqlite3.connect(str(comparisons_db))
        ic = con.execute("PRAGMA integrity_check").fetchall()
        qc = con.execute("PRAGMA quick_check").fetchall()
        wal = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
        con.close()
        ckpt["integrity_check"] = ic
        ckpt["quick_check"] = qc
        ckpt["wal_checkpoint"] = wal
        # Atomic write
        tmp = cp_path.with_suffix(cp_path.suffix + ".tmp")
        with open(tmp, "w") as f:
            json.dump(ckpt, f, indent=2, default=str)
        os.replace(tmp, cp_path)
        with open(ack_path, "w") as f:
            json.dump(ckpt, f, indent=2, default=str)
        return ckpt

    start = time.monotonic()
    deadline = start + args.duration_minutes * 60
    next_checkpoint_at = start + args.checkpoint_minutes * 60

    i = 0
    sampled_count = 0
    invariant_count = 0
    total_reads = 0
    enqueue_overheads: List[float] = []

    stop_requested = {"value": False, "reason": ""}
    def _sig_stop(signum, frame):
        stop_requested["value"] = True
        stop_requested["reason"] = f"signal {signum}"
    try:
        signal.signal(signal.SIGINT, _sig_stop)
        signal.signal(signal.SIGTERM, _sig_stop)
    except (ValueError, OSError):
        pass

    # Sleep so the worker (max_workers=1, Fuli p95 ~390ms) is not
    # artifically flooded. We pace at 1.0s, which yields roughly
    # 1800 enqueue attempts across the 30-min window. Real Fuli
    # throughput at the worker is ~7-8/min sustained plus
    # warmup outliers, so the actual admitted rate depends on how
    # fast the worker can drain. The pause is intentional: the
    # test asserts job 129, 256, 300 cross over, not that we max
    # out throughput. We slightly overshoot 300 to give the test
    # an internal buffer on transient Fuli timeouts.
    while time.monotonic() < deadline and not stop_requested["value"]:
        if args.stop_file and Path(args.stop_file).exists():
            stop_requested["value"] = True
            stop_requested["reason"] = "stop file"
            break
        telemetry: Dict[str, Any] = {}
        q = QUERY_SET[i % len(QUERY_SET)]
        i += 1
        total_reads += 1
        try:
            _ = shadow.handle_tool_call(
                "honcho_search",
                {"query": q, "top_k": 3},
                telemetry=telemetry,
            )
        except Exception as e:
            print(f"read failed: {e}")
            continue
        prim = telemetry.get("primary_result_sha256", "")
        ret = telemetry.get("returned_result_sha256", "")
        if prim and prim == ret:
            invariant_count += 1
        if telemetry.get("sampled"):
            sampled_count += 1
            if "enqueue_started_at_ms" in telemetry:
                enqueue_overheads.append(
                    telemetry["enqueue_completed_at_ms"]
                    - telemetry["enqueue_started_at_ms"]
                )
        if time.monotonic() >= next_checkpoint_at:
            checkpoint(reason="interval")
            next_checkpoint_at = time.monotonic() + args.checkpoint_minutes * 60
        time.sleep(1.0)

    final_ckpt = checkpoint(reason=stop_requested["reason"] or "duration_complete")
    final_ckpt["total_reads"] = total_reads
    final_ckpt["invariant_count"] = invariant_count
    final_ckpt["sampled_count"] = sampled_count
    # Bounded flush + decision-grade post-run snapshot. Re-use a
    # real accounting() read here (not the interval-checkpoint
    # envelope shape, which nests the dropped-queue counter under
    # ``queue.dropped_queue_full`` and would trip a KeyError on the
    # GO/NO-GO gate below).
    try:
        shadow.flush_comparisons(timeout_seconds=240.0)
    except Exception:
        pass
    final_acc = shadow.executor_accounting()
    final_ckpt["final_executor_accounting"] = final_acc
    final_ckpt["final_is_balanced"] = shadow.executor_is_balanced()
    dropped_queue_full = final_acc.get(
        "comparison_jobs_dropped_queue_full", 0
    )
    persisted_count = final_acc.get("comparison_jobs_persisted", 0)
    final_ckpt["decision"] = (
        "GO"
        if (
            final_ckpt["final_is_balanced"]
            and persisted_count >= 300
            and dropped_queue_full == 0
        )
        else "NO-GO"
    )
    final_ckpt["enqueue_overheads"] = {
        "p50": percentile(enqueue_overheads, 50) if enqueue_overheads else None,
        "p95": percentile(enqueue_overheads, 95) if enqueue_overheads else None,
        "max": max(enqueue_overheads) if enqueue_overheads else None,
    }
    final_ckpt["final_persisted_count"] = persisted_count
    final_ckpt["final_dropped_queue_full"] = dropped_queue_full
    with open(out_dir / "report.json", "w") as f:
        json.dump(final_ckpt, f, indent=2, default=str)

    if args.pid_file:
        try:
            Path(args.pid_file).unlink()
        except OSError:
            pass

    return 0 if final_ckpt["decision"] == "GO" else 2


if __name__ == "__main__":
    sys.exit(main())
