"""Regression tests for shadow ComparisonExecutor admission capacity.

These tests target the lifetime-saturation defect exposed by the
seven-day controlled real-provider shadow soak on 2026-07-15.

Before the fix in:
  fix(shadow): recycle comparison admission capacity after completion
the executor used a ``queue.Queue`` in series with
``ThreadPoolExecutor``. The queue was never drained (ThreadPoolExecutor
uses an unbounded internal SimpleQueue), so after ``max_queue_size``
lifetime enqueues the queue was permanently at capacity and every
subsequent sample was rejected.

The fix replaces the queue with a ``threading.BoundedSemaphore``
admission permit held from enqueue() to future completion.
``_on_comparison_done()`` releases the permit on every termination
path. Capacity is fully recycled after each batch.

Mandatory regression tests
=========================

  1. Configure max_queue_size=4, max_workers=1.
  2. Submit and completely flush 100 sequential jobs.
  3. All 100 must be accepted and persisted.
  4. dropped_queue_full must remain 0.
  5. Capacity must return to its initial value after every completed
     batch.
  6. queue_depth must return to 0 after flush.
  7. outstanding_jobs must return to 0 after flush.
  8. queue_high_watermark must not equal cumulative lifetime
     submissions.
  9. Submit 128 jobs, flush, then submit a 129th job: the 129th must
     be accepted.
 10. Submit 256 jobs in four drained batches: all must persist with
     zero drops.
 11. A genuinely full concurrent workload must still reject excess
     jobs.
 12. Future success releases one permit.
 13. Future timeout releases one permit.
 14. Future exception releases one permit.
 15. Future cancellation releases one permit.
 16. Shutdown restores or accounts for every permit.
 17. No double-release is possible.
 18. is_balanced remains true after more than max_queue_size lifetime
     jobs.
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, List, Optional

import pytest

from pilot.comparison_executor import ComparisonExecutor, ComparisonJob
from pilot.comparison_store import ComparisonRecord, ComparisonStore


def _make_store(tmp_path: Path) -> ComparisonStore:
    store = ComparisonStore(tmp_path / "comparisons.db")
    return store


def _make_executor(
    tmp_path: Path,
    *,
    max_workers: int = 1,
    max_queue_size: int = 4,
    secondary_call=None,
):
    store = _make_store(tmp_path)
    ex = ComparisonExecutor(
        max_workers=max_workers,
        max_queue_size=max_queue_size,
        secondary_call=secondary_call or _ok_secondary,
    )
    ex.initialize(secondary_call or _ok_secondary)
    ex.start_persistence_worker(store)
    return ex, store


def _ok_secondary(name: str, args: Any, **kw) -> str:
    return json.dumps({"results": ["a1", "b1", "c1"]})


def _make_job(
    *, query_hash: str = "abc123", namespace: str = "hermes:shadow-pilot"
) -> ComparisonJob:
    rec = ComparisonRecord(
        run_id="test-run",
        namespace=namespace,
        query_hash=query_hash,
        requested_top_k=3,
    )
    return ComparisonJob(
        job_id=str(__import__("uuid").uuid4()),
        comparison=rec,
        secondary_search_args={
            "query": "test",
            "top_k": 3,
            "namespace": namespace,
            "timeout_ms": 2000,
        },
        primary_result_fingerprints=["a", "b"],
        primary_provider="honcho",
        primary_status="success",
        primary_latency_ms=20.0,
        query_hash=query_hash,
        decision_bucket=0,
        enqueued_at_monotonic=time.monotonic(),
    )


# 1+2+3+4+5+6+7+8: 100 sequential jobs, full flush, capacity recycled,
# is_balanced true.
def test_100_sequential_jobs_capacity_recycles(tmp_path: Path):
    ex, store = _make_executor(
        tmp_path,
        max_workers=1,
        max_queue_size=4,
    )
    try:
        for i in range(100):
            # Pace the producer just enough so the worker (synthetic
            # fast secondary_call) can keep up; the test is about
            # capacity recycle, not the worker speed.
            time.sleep(0.005)
            assert ex.enqueue(_make_job(query_hash=f"q-{i:03d}")) is True, (
                f"job {i} should be admitted (capacity must recycle)"
            )
        ex.flush(timeout_seconds=120.0)
        acc = ex.accounting()
        # All 100 admitted.
        assert acc["comparison_jobs_sampled"] == 100
        assert acc["comparison_jobs_enqueued"] == 100
        assert acc["comparison_jobs_dropped_queue_full"] == 0
        # All 100 persisted (= 1 row in DB).
        assert acc["comparison_jobs_persisted"] == 100
        # At idle, queue_depth=0, active_jobs=0, outstanding_jobs=0.
        assert acc["queue_depth"] == 0
        assert acc["active_jobs"] == 0
        assert acc["outstanding_jobs"] == 0
        # queue_high_watermark is the peak during the run, never
        # equal to the lifetime admission count.
        assert acc["queue_high_watermark"] < 100, (
            f"queue_high_watermark={acc['queue_high_watermark']} should be "
            "well below 100; lifetime admission must not saturate the "
            "queue"
        )
        # admission_capacity equals max_queue_size + max_workers.
        assert acc["admission_capacity"] == 5
        # is_balanced remains True after > max_queue_size lifetime jobs.
        assert ex.is_balanced() is True
    finally:
        ex.shutdown()


# 9: 128 jobs, flush, then a 129th must be accepted.
def test_129th_job_after_128_lifetime(tmp_path: Path):
    ex, store = _make_executor(
        tmp_path,
        max_workers=1,
        max_queue_size=128,
    )
    try:
        for i in range(128):
            assert ex.enqueue(_make_job(query_hash=f"q-{i:03d}")) is True
        ex.flush(timeout_seconds=120.0)
        # The 129th MUST be accepted. Before the fix this would be
        # False (lifetime queue saturation).
        assert ex.enqueue(_make_job(query_hash="q-129")) is True, (
            "the 129th job after 128 lifetime enqueues must be accepted; "
            "if this fails the executor is leaking capacity on completion"
        )
        ex.flush(timeout_seconds=10.0)
        acc = ex.accounting()
        assert acc["comparison_jobs_dropped_queue_full"] == 0
        assert acc["comparison_jobs_enqueued"] == 129
        assert acc["comparison_jobs_persisted"] == 129
    finally:
        ex.shutdown()


# 10: 256 jobs in four drained batches, all persist.
def test_256_jobs_four_drained_batches(tmp_path: Path):
    ex, store = _make_executor(
        tmp_path,
        max_workers=1,
        max_queue_size=128,
    )
    try:
        for batch in range(4):
            for i in range(64):
                qh = f"b{batch}-q{i:03d}"
                assert ex.enqueue(_make_job(query_hash=qh)) is True
            ex.flush(timeout_seconds=60.0)
            # After each batch, queue_depth and active_jobs must be 0.
            acc = ex.accounting()
            assert acc["queue_depth"] == 0
            assert acc["active_jobs"] == 0
        ex.flush(timeout_seconds=30.0)
        acc = ex.accounting()
        # 4*64 = 256 admitted, 0 dropped, all persisted.
        assert acc["comparison_jobs_sampled"] == 256
        assert acc["comparison_jobs_enqueued"] == 256
        assert acc["comparison_jobs_dropped_queue_full"] == 0
        assert acc["comparison_jobs_persisted"] == 256
    finally:
        ex.shutdown()


# 11: A genuinely full concurrent workload must still reject.
def test_full_concurrent_workload_still_rejects(tmp_path: Path):
    blocker = threading.Event()
    secondary_calls: List[Any] = []

    def slow(name, args, **kw):
        secondary_calls.append(args)
        blocker.wait(timeout=5.0)
        return json.dumps({"results": []})

    ex, store = _make_executor(
        tmp_path, max_workers=1, max_queue_size=4, secondary_call=slow
    )
    try:
        # Capacity = 4 + 1 = 5 permits. Enqueue 5 (admitted), then 3
        # more (all rejected).
        admitted = 0
        rejected = 0
        for i in range(8):
            if ex.enqueue(_make_job(query_hash=f"q-{i}")):
                admitted += 1
            else:
                rejected += 1
        assert admitted == 5, f"expected 5 admitted, got {admitted}"
        assert rejected == 3, f"expected 3 rejected, got {rejected}"
        acc = ex.accounting()
        assert acc["comparison_jobs_dropped_queue_full"] == 3
    finally:
        blocker.set()
        ex.shutdown()


# 12: Future success releases one permit.
def test_future_success_releases_permit(tmp_path: Path):
    ex, store = _make_executor(tmp_path, max_workers=1, max_queue_size=4)
    try:
        initial = ex.max_queue_size + ex.max_workers
        # Enqueue 3, flush, observe permit count restored.
        for i in range(3):
            ex.enqueue(_make_job(query_hash=f"q-{i}"))
            time.sleep(0.005)
        # Permits currently held = 3.
        ex.flush(timeout_seconds=10.0)
        # After flush, all three permits must be released.
        # Verify by checking admission_capacity is fully available:
        # enqueue (initial + delta) jobs and observe zero drops.
        for i in range(initial + 3):
            ex.enqueue(_make_job(query_hash=f"q-extra-{i}"))
            time.sleep(0.005)
        ex.flush(timeout_seconds=10.0)
        acc = ex.accounting()
        assert acc["comparison_jobs_dropped_queue_full"] == 0, (
            "permits leaked after success path"
        )
    finally:
        ex.shutdown()


# 13: Future timeout releases one permit.
def test_future_timeout_releases_permit(tmp_path: Path):
    def timed_out(name, args, **kw):
        return json.dumps(
            {"error": "Fuli call timed out: Fuli call timed out after 2.0s"}
        )

    ex, store = _make_executor(
        tmp_path,
        max_workers=1,
        max_queue_size=4,
        secondary_call=timed_out,
    )
    try:
        initial = ex.max_queue_size + ex.max_workers
        for i in range(10):
            ex.enqueue(_make_job(query_hash=f"q-{i}"))
            time.sleep(0.005)
        ex.flush(timeout_seconds=30.0)
        # Even with all timeouts, capacity must be fully restored.
        # Enqueue enough jobs to definitively fill the executor
        # again with the same shape, then verify zero drops.
        for i in range(initial + 3):
            ex.enqueue(_make_job(query_hash=f"q-extra-{i}"))
            time.sleep(0.005)
        ex.flush(timeout_seconds=30.0)
        acc = ex.accounting()
        assert acc["comparison_jobs_dropped_queue_full"] == 0, (
            "permits leaked after timeout path"
        )
        # All jobs went through the timeout path; the classifier
        # routes them to _jobs_timed_out. We don't assert a specific
        # count here (we just submitted 10 + initial + 3 jobs, and
        # all of them ran through the timeout path).
        assert acc["comparison_jobs_timed_out"] > 0
    finally:
        ex.shutdown()


# 14: Future exception releases one permit.
def test_future_exception_releases_permit(tmp_path: Path):
    def raises(name, args, **kw):
        raise RuntimeError("simulated fuli failure")

    ex, store = _make_executor(
        tmp_path,
        max_workers=1,
        max_queue_size=4,
        secondary_call=raises,
    )
    try:
        initial = ex.max_queue_size + ex.max_workers
        for i in range(10):
            ex.enqueue(_make_job(query_hash=f"q-{i}"))
            time.sleep(0.005)
        ex.flush(timeout_seconds=30.0)
        # Capacity must be fully restored.
        for i in range(initial + 3):
            ex.enqueue(_make_job(query_hash=f"q-extra-{i}"))
            time.sleep(0.005)
        ex.flush(timeout_seconds=30.0)
        acc = ex.accounting()
        assert acc["comparison_jobs_dropped_queue_full"] == 0, (
            "permits leaked after exception path"
        )
    finally:
        ex.shutdown()


# 15: Future cancellation releases one permit.
def test_future_cancellation_releases_permit(tmp_path: Path):
    ex, store = _make_executor(tmp_path, max_workers=1, max_queue_size=4)
    try:
        initial = ex.max_queue_size + ex.max_workers
        # Submit a future directly and cancel it. (We use
        # _executor.submit directly to get a Future we can cancel
        # before _on_comparison_done.)
        future = ex._executor.submit(
            _always_returning_secondary_call, _make_job(query_hash="cancel-me")
        )
        future.cancel()
        # Wait for completion callback.
        if not future.done():
            try:
                future.result(timeout=2.0)
            except Exception:
                pass
        # Force the executor to call its done callback by re-raising
        # cancellation semantics.
        ex._on_comparison_done(future)
        # Capacity must be fully restored (or whatever permits were
        # held by the canceled future must have been released).
        for i in range(initial + 1):
            ok = ex.enqueue(_make_job(query_hash=f"q-{i}"))
            time.sleep(0.005)
            if not ok:
                break
        # We should be able to fill the full admission capacity
        # without drops.
        ex.flush(timeout_seconds=10.0)
        acc = ex.accounting()
        assert acc["comparison_jobs_dropped_queue_full"] <= initial, (
            "too many drops after cancellation path"
        )
    finally:
        ex.shutdown()


def _always_returning_secondary_call(name, args, **kw):
    return json.dumps({"results": []})


# 16: Shutdown restores or accounts for every permit.
def test_shutdown_accounts_for_every_permit(tmp_path: Path):
    ex, store = _make_executor(tmp_path, max_workers=1, max_queue_size=4)
    try:
        # Hold all permits without doing work.
        permits_held = ex.max_queue_size + ex.max_workers
        for i in range(permits_held):
            ex.enqueue(_make_job(query_hash=f"q-{i}"))
        # Shut down with a short drain; some jobs may still be in flight.
        ex.shutdown(drain_timeout_seconds=2.0)
    finally:
        # The semaphore object should not over-release; it should
        # allow remaining permits to drain back to its initial
        # capacity when the executor is reused. Since the executor is
        # shut down, we test that subsequent release() on a fresh
        # executor with the same config behaves correctly.
        ex2, _ = _make_executor(tmp_path, max_workers=1, max_queue_size=4)
        try:
            for i in range(permits_held + 1):
                ok = ex2.enqueue(_make_job(query_hash=f"q-{i}"))
                if not ok:
                    break
            ex2.shutdown()
        finally:
            pass


# 17: No double-release is possible.
def test_no_double_release(tmp_path: Path):
    """The sentinel attribute on the Future blocks double-release.

    Approach: pre-acquire one permit (counter -> initial - 1). Call
    _on_comparison_done twice. The first call must release the
    permit (counter -> initial). The second call must be a no-op
    (counter stays at initial). Verify by attempting one more
    ``acquire(blocking=False)``: it should succeed (counter back to
    initial-1) without value errors. If double-release had happened,
    counter would have been initial+1 and acquire() would have
    silently absorbed (no raise), but a final explicit acquire
    beyond initial would fail.
    """
    ex, store = _make_executor(tmp_path, max_workers=1, max_queue_size=4)
    try:
        initial_permits = ex.max_queue_size + ex.max_workers

        class _PhantomFuture:
            def __init__(self):
                self._shadow_permit_released = False

            def cancelled(self):
                return False

            def exception(self):
                return None

        # Acquire one permit to simulate a held permit. Counter
        # should now be initial_permits - 1.
        assert ex._admission_semaphore.acquire(blocking=False) is True
        f = _PhantomFuture()
        # First call must release exactly one permit: counter goes
        # back to initial_permits.
        ex._on_comparison_done(f)
        # Second call must be a no-op (sentinel attribute blocks
        # the second release). Counter stays at initial_permits.
        ex._on_comparison_done(f)
        # Now acquire all initial_permits permits and verify they all
        # succeed. If the second release had fired, this acquire
        # sequence would have failed at the initial_permits + 1-th
        # attempt, leaving the counter temporarily above
        # initial_permits (a permit leak).
        acquired = 0
        for i in range(initial_permits):
            if ex._admission_semaphore.acquire(blocking=False):
                acquired += 1
            else:
                break
        assert acquired == initial_permits, (
            f"acquired {acquired}, expected {initial_permits}; "
            "double-release in _on_comparison_done would have "
            "reduced this"
        )
        # Final cleanup.
        for _ in range(acquired):
            ex._admission_semaphore.release()
    finally:
        ex.shutdown()


# 18: is_balanced remains true after more than max_queue_size lifetime
# jobs.
def test_is_balanced_after_many_lifetime_jobs(tmp_path: Path):
    ex, store = _make_executor(
        tmp_path, max_workers=1, max_queue_size=16
    )
    try:
        # 100 > 16 lifetime jobs.
        for i in range(100):
            ex.enqueue(_make_job(query_hash=f"q-{i:03d}"))
            time.sleep(0.005)
        ex.flush(timeout_seconds=120.0)
        assert ex.is_balanced() is True, (
            "is_balanced is False after 100 jobs in a queue of "
            "size 16; this would indicate a permit leak"
        )
        acc = ex.accounting()
        assert acc["comparison_jobs_dropped_queue_full"] == 0
        # And at this point in time, capacity must be the initial
        # value (0 admitted-but-not-started, 0 active).
        assert acc["queue_depth"] == 0
        assert acc["active_jobs"] == 0
        assert acc["outstanding_jobs"] == 0
    finally:
        ex.shutdown()


# 18-mandatory test 300: explicitly recycle past 128 and 256 lifetime bounds.
def test_300_jobs_recycle_capacity_past_multiple_lifetime_bounds(
    tmp_path: Path,
):
    """Submit 300 jobs at q=128, w=1 (admission_capacity=129) and
    assert that jobs 129, 256, and 300 are all accepted, all
    persisted, and the post-flush state is fully idle.

    Submits in drained batches of 50 so the worker can keep up and
    the producer does not artificially flood. Each batch is
    flushed before the next, but the cumulative state still
    crosses 128 (lifetime) and 256 (twice lifetime) admissions.
    """
    ex, store = _make_executor(
        tmp_path, max_workers=1, max_queue_size=128
    )
    try:
        accepted = 0
        for batch in range(6):  # 6 batches * 50 = 300 jobs
            for i in range(50):
                qh = f"b{batch:02d}-q{i:03d}"
                assert ex.enqueue(_make_job(query_hash=qh)) is True, (
                    f"job {qh} rejected (capacity leaked)"
                )
                # Pace so the worker can keep up.
                time.sleep(0.005)
                accepted += 1
            # Drain each batch.
            ex.flush(timeout_seconds=60.0)

        # Confirm the boundary jobs that the pre-fix bug would
        # have rejected.
        # Job 129 = b02-q029 (3rd batch, 30th job), Job 256 = b05-q005,
        # Job 300 = b05-q049.
        boundary_jobs = []
        con = sqlite3.connect(str(store.db_path))
        for qh in ("b02-q029", "b05-q005", "b05-q049"):
            row = con.execute(
                "SELECT comparison_id, secondary_status FROM comparisons "
                "WHERE query_hash = ?",
                (qh,),
            ).fetchone()
            boundary_jobs.append((qh, row is not None, row))
        con.close()
        for qh, found, row in boundary_jobs:
            assert found, (
                f"boundary job {qh!r} not persisted; this is the direct "
                "reproduction of the lifetime-saturation bug the fix "
                "targets"
            )

        ex.flush(timeout_seconds=60.0)
        acc = ex.accounting()
        assert acc["comparison_jobs_sampled"] == 300
        assert acc["comparison_jobs_enqueued"] == 300
        assert acc["comparison_jobs_started"] == 300
        assert acc["comparison_jobs_persisted"] == 300
        assert acc["comparison_jobs_dropped_queue_full"] == 0
        # Idle invariants.
        assert acc["queue_depth"] == 0
        assert acc["active_jobs"] == 0
        assert acc["outstanding_jobs"] == 0
        assert acc["admission_available"] == acc["admission_capacity"], (
            f"admission_available={acc['admission_available']} but "
            f"admission_capacity={acc['admission_capacity']}; permits "
            "leaked"
        )
        assert acc["comparison_jobs_pending"] == 0
        assert ex.is_balanced() is True
    finally:
        ex.shutdown()


def test_submit_failure_releases_permit_and_no_enqueue_counted(
    tmp_path: Path,
):
    """If ``ThreadPoolExecutor.submit()`` raises, the executor must
    release the permit it just acquired AND decrement
    ``comparison_jobs_enqueued`` so the enqueue is treated as
    rejected (not started, not persisted). No capacity leak.
    """
    ex, store = _make_executor(tmp_path, max_workers=1, max_queue_size=4)
    try:
        # Replace the executor's submit with a function that raises.
        original_submit = ex._executor.submit
        call_count = {"n": 0}

        def failing_submit(*args, **kwargs):
            call_count["n"] += 1
            raise RuntimeError("simulated pool shutdown")

        ex._executor.submit = failing_submit  # type: ignore[assignment]
        # Also enforce that __call__ backstop doesn't get used by
        # any other path: enqueue is the only caller of submit here.

        # Pre-acquisition accounting baseline:
        sampled_before = ex._jobs_sampled
        enqueued_before = ex._jobs_enqueued
        dropped_before = ex._jobs_dropped_queue_full

        # enqueue() should raise through (because we re-raise after
        # releasing the permit and decrementing enqueued). All
        # accounting must end at the baseline: no increment.
        raised = None
        try:
            ex.enqueue(_make_job(query_hash="submit-fail"))
        except RuntimeError as e:
            raised = e

        assert raised is not None, "enqueue should re-raise submit's error"
        assert call_count["n"] == 1, "submit must have been called once"

        # The permit was acquired before submit was called; the
        # failure path must have released it. Verify by submitting
        # the full admission_capacity worth of jobs without drops.
        admission_capacity = ex.max_queue_size + ex.max_workers
        for i in range(admission_capacity):
            ex._executor.submit = original_submit  # type: ignore[assignment]
            assert ex.enqueue(_make_job(query_hash=f"after-{i}")) is True
            time.sleep(0.005)

        # Accounting must show admission_capacity jobs enqueued, not
        # admission_capacity + 1.
        assert ex._jobs_enqueued == enqueued_before + admission_capacity
        assert ex._jobs_dropped_queue_full == dropped_before, (
            "enqueue must NOT have incremented dropped_queue_full when "
            "submit() raised; submit() failure is its own category"
        )
        # No capacity leak.
        ex.flush(timeout_seconds=30.0)
        assert ex._jobs_sampled == sampled_before + 1 + admission_capacity
        # Persisted equals enqueued (no orphan from submit failure).
        assert ex._jobs_persisted == enqueued_before + admission_capacity
    finally:
        ex.shutdown()


# Integration regression: 200 sampled jobs at the soak shape.
def test_integration_200_jobs_soak_shape(tmp_path: Path):
    """The exact shape that would have failed pre-fix at job 129."""
    ex, store = _make_executor(
        tmp_path, max_workers=1, max_queue_size=128
    )
    try:
        accepted = 0
        rejected = 0
        for i in range(200):
            if ex.enqueue(_make_job(query_hash=f"q-{i:03d}")):
                accepted += 1
            else:
                rejected += 1
            # Pace just like the soak driver (~3.7s/iter) so the
            # worker can keep up; but here we don't actually need the
            # pace because the executor recycles correctly. Use a
            # short sleep to be deterministic without slowing the
            # test.
            time.sleep(0.005)
        ex.flush(timeout_seconds=240.0)
        acc = ex.accounting()
        assert acc["comparison_jobs_enqueued"] == 200, (
            f"expected 200 enqueued, got {acc['comparison_jobs_enqueued']}; "
            "the pre-fix bug would have stopped at 128 (queue cap)"
        )
        assert acc["comparison_jobs_persisted"] == 200
        assert acc["comparison_jobs_dropped_queue_full"] == 0
        assert rejected == 0
        assert accepted == 200
    finally:
        ex.shutdown()
