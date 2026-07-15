"""Bounded background comparison executor for the shadow pilot.

Architecture
============

The executor owns two fixed-size worker pools and a single shared
persistence worker:

  ┌────────────────┐    bounded queue   ┌──────────────────┐
  │  foreground    │ ────job──────▶     │  comparison      │
  │  handle_tool_  │     maxsize=       │  workers         │
  │  call          │     max_queue_size │  (max_workers)   │
  └────────────────┘                    └──────────────────┘
                                                  │
                                                  │ puts ComparisonRecord
                                                  ▼
                                          ┌──────────────────┐
                                          │  persistence     │
                                          │  worker          │
                                          │  (1 thread)      │
                                          └──────────────────┘
                                                  │
                                                  ▼
                                          ┌──────────────────┐
                                          │  ComparisonStore │
                                          │  (SQLite WAL)    │
                                          └──────────────────┘

Boundedness invariant
=====================

  total outstanding jobs  ≤  max_queue_size + max_workers

This is enforced because:

  - ``enqueue()`` non-blockingly acquires one permit from
    ``self._admission_semaphore``, which is initialized with
    ``max_queue_size + max_workers`` permits. ThreadPoolExecutor
    uses an unbounded internal ``SimpleQueue``, so we cannot rely
    on it for admission control; we use a ``BoundedSemaphore``
    instead. The permit is released exactly once per accepted job,
    on every termination path (success / timeout / exception /
    cancellation), inside ``_on_comparison_done``.
  - ``_executor`` is a ``ThreadPoolExecutor(max_workers=max_workers)``.
    Up to ``max_workers`` jobs may run concurrently.
  - The pool's internal SimpleQueue may grow without bound, but our
    admission semaphore guarantees that no more than
    ``max_queue_size + max_workers`` jobs are ever submitted before
    a corresponding release. Net outstanding is therefore bounded
    at ``max_queue_size + max_workers``.
  - ``enqueue()`` either succeeds (permit acquired + future
    submitted) or returns False (no permit available;
    ``dropped_queue_full`` incremented).
  - There is NO spawning of per-job threads inside the worker. The
    worker calls Fuli synchronously; Fuli enforces its own per-call
    timeout via its async bridge. The worker stays parked on the
    call; it cannot create additional threads.
  - ``_persistence_queue`` is unbounded, but it is drained by a SINGLE
    thread, so its growth rate is bounded by the rate at which
    comparisons finish.

Thread safety
=============

Every counter mutation, flag read/write, queue-size read, and
high-watermark update happens under ``self._lock``. Snapshots
returned by ``accounting()`` represent one consistent point in time.

Job immutability and privacy
============================

A ``ComparisonJob`` carries:

  - ``comparison_id`` (auto-assigned by the store on persist)
  - ``query_hash`` (SHA-256[:16] of the query text)
  - ``primary_provider``, ``primary_status``, ``primary_latency_ms``
  - ``primary_result_fingerprints`` (a list of 16-char hex strings;
    the actual primary response text is NOT carried)
  - ``requested_top_k``
  - ``namespace``
  - ``run_id``
  - ``decision_bucket``
  - ``enqueued_at_monotonic``
  - ``secondary_search_args`` — the *minimum* arguments required to
    call Fuli's ``fuli_memory_search``. This includes the raw query
    text because Fuli cannot execute a search on a hash. The query
    text is therefore in-memory but is **NEVER** written to logs,
    exception messages, or persistence. The persisted row uses
    ``query_hash`` instead.

The raw primary response text is dropped at enqueue time. It is not
recoverable from the job.

Timeout / cancellation
======================

Python cannot forcibly terminate a running thread. The comparison
worker therefore relies on Fuli's own per-call timeout, propagated via
``secondary_search_args['timeout_ms'] = comparison_budget_ms``. If the
Fuli bridge's timeout fires, the worker's ``handle_tool_call`` returns
and the worker marks the job ``secondary_status='failed'`` with
``secondary_error_category='timeout_after_Xms'``. If the Fuli bridge
fails to honor its timeout (a known Fuli hang), the comparison worker
stays blocked on the call; the foreground and other comparison jobs
are unaffected because there is a single fixed-size worker pool.

The executor tracks these as observable evidence: a comparison that
exceeds ``comparison_budget_ms`` by more than ``ORPHAN_GRACE_SECONDS``
is counted in a ``comparison_jobs_orphaned`` accounting field.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from pilot.comparison_store import ComparisonRecord, ComparisonStore, ComparisonStoreError

logger = logging.getLogger(__name__)

# When a comparison worker is blocked on a Fuli call, we cannot
# forcibly terminate it. We measure how long it has been blocked and
# surface this as observable evidence (not as a failure). Anything
# that exceeds comparison_budget_ms + ORPHAN_GRACE_SECONDS gets
# counted in comparison_jobs_orphaned so an operator can see the
# leakage. The worker will eventually return (when Fuli eventually
# responds) and the job will be persisted with the actual latency.
ORPHAN_GRACE_SECONDS: float = 5.0


@dataclass
class ComparisonJob:
    """Immutable input to a background comparison.

    The foreground constructs this object on the request thread and
    never mutates it. All fields are JSON-serializable scalars or
    lists. The ``secondary_search_args`` field carries the raw query
    text needed for Fuli to execute; this is in-memory only and is
    NEVER logged or persisted.
    """

    job_id: str
    comparison: ComparisonRecord
    secondary_search_args: Dict[str, Any]
    primary_result_fingerprints: list  # List[str], pre-computed on the foreground
    primary_provider: str
    primary_status: str
    primary_latency_ms: float
    query_hash: str
    decision_bucket: int
    enqueued_at_monotonic: float

    def age_seconds(self, now_monotonic: Optional[float] = None) -> float:
        return (now_monotonic or time.monotonic()) - self.enqueued_at_monotonic


def _is_timeout_error(category: Optional[str]) -> bool:
    """Return True iff ``category`` describes a secondary-side timeout.

    The matcher is normalize-then-substring: it lowercases, replaces
    underscores with spaces, and collapses internal whitespace so the
    common envelope variations all collapse to a stable form. It
    recognizes:

      - "timeout"           (noun used by most error envelopes)
      - "timed out"         (verb used by Fuli: "Fuli call timed
                             out after 2.0s")
      - "deadline exceeded"  (Fuli async-bridge fallback under load)

    The previous substring-only check ``"timeout" in category``
    case-folded only the LHS, so a Fuli envelope of the form
    "Fuli call timed out after 2.0s" was misclassified as a
    generic failure. This helper fixes that class of bug.
    """
    if not category:
        return False
    normalized = " ".join(category.lower().replace("_", " ").split())
    return (
        "timeout" in normalized
        or "timed out" in normalized
        or "deadline exceeded" in normalized
    )


class ComparisonExecutor:
    """Bounded background executor for sampled-read comparisons.

    Construction parameters (all have sensible defaults):
      max_workers: int = 1
        Number of comparison workers. Keep at 1 unless profiling shows
        the Fuli async bridge loop has spare capacity.
      max_queue_size: int = 1024
        Maximum number of queued comparison jobs. ``enqueue()`` returns
        False when the queue is full; the caller MUST record a
        ``comparison_jobs_dropped_queue_full`` accounting event.
      comparison_budget_ms: int = 2000
        Wall-clock cap on each Fuli call. Lives only inside the
        background worker; never affects the foreground request.

    Thread safety:
      All accounting counters are updated under ``self._lock``. The
      ``_persistence_queue`` is a thread-safe ``queue.Queue``.
    """

    def __init__(
        self,
        *,
        max_workers: int = 1,
        max_queue_size: int = 1024,
        comparison_budget_ms: int = 2000,
        secondary_call: Optional[Callable[..., str]] = None,
    ) -> None:
        self.max_workers = max(1, max_workers)
        self.max_queue_size = max(1, max_queue_size)
        self.comparison_budget_ms = max(50, comparison_budget_ms)
        self._secondary_call = secondary_call  # injected for tests

        # Admission control. We DO NOT use a queue.Queue for admission
        # because ``ThreadPoolExecutor`` consumes its own tasks via an
        # unbounded internal SimpleQueue. A bounded ``queue.Queue``
        # placed in series would never be drained and would saturate
        # after ``maxsize`` lifetime enqueues, permanently rejecting
        # all further work. Instead, we admit work via a non-blocking
        # ``BoundedSemaphore``: ``enqueue()`` tries to acquire one
        # permit; if it cannot, the producer is told the executor is
        # at capacity (and increments ``dropped_queue_full``). The
        # permit is held until the future finishes, on every
        # termination path (success / timeout / exception /
        # cancellation). This guarantees ``queue_depth`` and
        # ``active_jobs`` come back to zero after every batch and
        # capacity is fully recycled.
        #
        # Total outstanding jobs at any moment is therefore strictly
        # bounded by ``max_queue_size + max_workers`` (queue depth
        # plus in-flight worker jobs).
        self._admission_semaphore = threading.BoundedSemaphore(
            value=self.max_queue_size + self.max_workers
        )

        # Persistence queue is unbounded but drained by a single
        # daemon thread, so its growth rate is bounded by the rate at
        # which comparisons finish.
        self._persistence_queue: "queue.Queue[ComparisonJob]" = queue.Queue()

        # Workers. ``ThreadPoolExecutor`` uses an unbounded internal
        # ``SimpleQueue`` for its work queue; we do NOT rely on it
        # for boundedness. The pool size is fixed at ``max_workers``;
        # we tolerate up to ``max_queue_size`` queued (waiting) jobs
        # in addition to ``max_workers`` in-flight jobs.
        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="shadow-compare",
        )
        self._persistence_thread: Optional[threading.Thread] = None

        # Lifecycle.
        self._accepting = True
        self._stopped = False
        # Explicit stop event so the persistence worker can exit
        # promptly even if the underlying ThreadPoolExecutor shutdown
        # is bypassed or times out. Without this, the persistence
        # worker's infinite loop keeps the executor's thread pool
        # alive forever, leaking across tests.
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Watermark tracking. Guarded by self._lock.
        self._queue_high_watermark = 0  # max admitted-but-not-running
        self._active_high_watermark = 0  # max running concurrently
        self._outstanding_high_watermark = 0  # max (queue + active)
        self._enqueued_futures: set = set()

        # Accounting counters. Guarded by self._lock.
        self._jobs_sampled = 0
        self._jobs_enqueued = 0
        self._jobs_started = 0
        self._jobs_completed = 0
        self._jobs_timed_out = 0
        self._jobs_failed = 0
        self._jobs_dropped_queue_full = 0
        self._jobs_persisted = 0
        self._persistence_failed = 0
        self._persistence_started = 0
        self._duplicate_idempotent = 0
        self._unexpected_collision = 0
        # Orphaned Fuli calls: a comparison worker has been blocked on
        # a Fuli call for more than comparison_budget_ms +
        # ORPHAN_GRACE_SECONDS. The worker is still alive but the
        # foreground is unaffected.
        self._jobs_orphaned = 0

    # --- initialization helpers ----------------------------------------

    def initialize(self, secondary_call: Callable[..., str]) -> None:
        """Inject the secondary provider's call."""
        self._secondary_call = secondary_call

    # --- enqueue / foreground contract ---------------------------------

    def enqueue(self, job: ComparisonJob) -> bool:
        """Enqueue a comparison job. Returns True if accepted, False if
        the executor is at capacity.

        The foreground thread MUST NOT block here. The admission
        semaphore is non-blocking: ``acquire(blocking=False)`` returns
        False if no permit is available, and we report the rejected
        enqueue as ``comparison_jobs_dropped_queue_full``.

        Accounting identity (every accepted job corresponds to one
        permit held from this call until the future completes):
            sampled == enqueued + dropped
            sampled - (enqueued + dropped) == 0  # holds per-batch
            permitted == enqueued
        """
        # Read-and-mutate under the lock to avoid GIL races on
        # _accepting and the dropped counter.
        with self._lock:
            if not self._accepting:
                self._jobs_dropped_queue_full += 1
                return False
            self._jobs_sampled += 1
            # Non-blocking permit acquire. The permit is held until
            # _on_comparison_done() releases it (success, timeout,
            # exception, or cancellation).
            if not self._admission_semaphore.acquire(blocking=False):
                self._jobs_dropped_queue_full += 1
                # IMPORTANT: never pair put_nowait with this; we no
                # longer touch any queue here. Only the semaphore
                # governs admission.
                return False
            self._jobs_enqueued += 1
            # Track queue_depth = enqueued - started.
            # We don't have a separate queue.Queue; the depth IS the
            # admitted-but-not-yet-running gap.
            depth = max(
                0,
                self._jobs_enqueued
                - self._jobs_started
                - self._jobs_completed
                - self._jobs_timed_out
                - self._jobs_failed,
            )
            if depth > self._queue_high_watermark:
                self._queue_high_watermark = depth
            # Submit to the bounded pool. NOTE: ThreadPoolExecutor
            # has an unbounded internal SimpleQueue, so submit() does
            # not normally block. We rely on the admission semaphore
            # for boundedness; the pool size limits concurrent running
            # jobs to max_workers. If submit() ever raises (e.g. pool
            # shutdown), we must release the permit we just acquired
            # so capacity does not leak. We do that with an explicit
            # try/except that DECREMENTS _jobs_enqueued before
            # releasing, so the admission balance remains exactly
            # conserved.
            try:
                future = self._executor.submit(self._run_comparison, job)
            except Exception:
                self._jobs_enqueued -= 1
                # Drop the permit we acquired so admission_capacity
                # is fully restored.
                try:
                    self._admission_semaphore.release()
                except ValueError:
                    logger.exception(
                        "shadow comparison executor semaphore "
                        "over-release on submit failure"
                    )
                raise
            self._enqueued_futures.add(future)
        future.add_done_callback(self._on_comparison_done)
        return True

    def _on_comparison_done(self, future: Future) -> None:
        """Release the admission semaphore exactly once on every
        termination path.

        All terminal counters (completed / timed_out / failed) are
        incremented inside ``_run_comparison``'s finally-like path.
        This callback exists exclusively to release the permit.
        Without it, capacity is permanently leaked after every batch
        and the executor reaches saturation.

        Defensive ordering: the permit release runs in a ``finally``
        block so that any error in accounting or ``_enqueued_futures``
        bookkeeping CANNOT bypass the release. The sentinel attribute
        guards against duplicate callbacks firing release twice.
        """
        cancelled = False
        try:
            cancelled = future.cancelled()
        except Exception:
            cancelled = False
        try:
            released = getattr(future, "_shadow_permit_released", False)
        except Exception:
            released = False
        if not released:
            try:
                setattr(future, "_shadow_permit_released", True)
            except Exception:
                pass
            try:
                self._admission_semaphore.release()
            except ValueError:
                # BoundedSemaphore.release() raises ValueError if the
                # counter is already at capacity. Should not happen if
                # _enqueue/_release are paired correctly; surface it.
                logger.exception(
                    "shadow comparison executor semaphore over-release"
                )
        # The bookkeeping below is best-effort. If it raises (e.g.
        # due to a threadpool shutdown interaction) we do NOT want to
        # skip the bookkeeping on subsequent callbacks; but the
        # permit release has already happened above, so we are not
        # leaking capacity.
        try:
            with self._lock:
                self._enqueued_futures.discard(future)
                if cancelled:
                    # Cancellation counts as failed (no normal
                    # completion path ran). Python's Future.cancel()
                    # only succeeds on a future that has not started,
                    # so this branch is taken only when the future
                    # never reached _run_comparison. Therefore it is
                    # safe to increment _jobs_failed here exactly
                    # once.
                    self._jobs_failed += 1
                    return
            exc = future.exception()
            if exc is not None:
                # _run_comparison's exception handler already
                # incremented the counter.
                logger.debug("comparison future raised: %s", exc)
        except Exception:
            logger.exception(
                "shadow comparison executor bookkeeping error after "
                "permit release"
            )

    # --- worker (runs in a thread pool) -------------------------------

    def _run_comparison(self, job: ComparisonJob) -> None:
        """Execute one comparison job: Fuli call, build record, persist.

        Called on a thread-pool worker. All exceptions are caught and
        recorded via accounting; the foreground never sees them.

        This method does NOT spawn additional threads. The Fuli call
        runs synchronously on the worker thread; Fuli's own async
        bridge enforces the per-call timeout.
        """
        try:
            with self._lock:
                self._jobs_started += 1
                # active_jobs = started - (completed + timed_out + failed).
                active = max(
                    0,
                    self._jobs_started
                    - self._jobs_completed
                    - self._jobs_timed_out
                    - self._jobs_failed,
                )
                if active > self._active_high_watermark:
                    self._active_high_watermark = active
                # Recompute outstanding high-watermark.
                depth = max(
                    0,
                    self._jobs_enqueued
                    - self._jobs_started
                    - self._jobs_completed
                    - self._jobs_timed_out
                    - self._jobs_failed,
                )
                outstanding = depth + active
                if outstanding > self._outstanding_high_watermark:
                    self._outstanding_high_watermark = outstanding
            comparison = job.comparison
            queue_wait_ms = job.age_seconds() * 1000.0
            sec_start = time.monotonic()
            secondary_raw, secondary_error_category = self._call_secondary(
                job.secondary_search_args
            )
            secondary_latency_ms = (time.monotonic() - sec_start) * 1000.0

            # Orphan detection: if the Fuli call took longer than the
            # budget + grace period, count it as orphaned. This is
            # observable evidence, not a failure of the job itself.
            budget_s = self.comparison_budget_ms / 1000.0
            if (
                secondary_error_category is None
                and secondary_latency_ms / 1000.0
                > budget_s + ORPHAN_GRACE_SECONDS
            ):
                with self._lock:
                    self._jobs_orphaned += 1

            if secondary_error_category is None:
                comparison.secondary_status = "success"
            else:
                comparison.secondary_status = "failed"
            comparison.secondary_latency_ms = secondary_latency_ms
            comparison.queue_wait_ms = queue_wait_ms
            comparison.secondary_error_category = secondary_error_category
            comparison.secondary_retrieval_mode = self._detect_retrieval_mode(
                job.secondary_search_args
            )

            # Compute fingerprints + metrics off the raw result.
            secondary_results = self._parse_secondary_results(secondary_raw)
            from pilot.comparison_metrics import (
                missing_from,
                overlap_at_k,
                reciprocal_rank_agreement,
                result_fingerprints,
            )
            primary_fps = job.primary_result_fingerprints
            secondary_fps = (
                result_fingerprints(secondary_results, limit=comparison.requested_top_k)
                if secondary_results is not None
                else []
            )
            comparison.primary_result_fingerprints = primary_fps
            comparison.secondary_result_fingerprints = secondary_fps
            comparison.overlap_at_1 = overlap_at_k(primary_fps, secondary_fps, 1)
            comparison.overlap_at_3 = overlap_at_k(primary_fps, secondary_fps, 3)
            comparison.overlap_at_5 = overlap_at_k(
                primary_fps, secondary_fps, comparison.requested_top_k
            )
            comparison.reciprocal_rank_agreement = reciprocal_rank_agreement(
                primary_fps, secondary_fps
            )
            comparison.missing_from_primary = missing_from(
                primary_fps, secondary_fps, comparison.requested_top_k
            )
            comparison.missing_from_secondary = missing_from(
                secondary_fps, primary_fps, comparison.requested_top_k
            )
            comparison.content_captured = False

            with self._lock:
                if _is_timeout_error(secondary_error_category):
                    self._jobs_timed_out += 1
                elif secondary_error_category:
                    self._jobs_failed += 1
                else:
                    self._jobs_completed += 1
            self._persistence_queue.put(job)

        except Exception as exc:
            with self._lock:
                self._jobs_failed += 1
            logger.warning(
                "ComparisonExecutor._run_comparison failed for job_id=%s "
                "query_hash=%s run_id=%s: %s",
                job.job_id,
                job.query_hash,
                job.comparison.run_id,
                exc,
            )

    def _call_secondary(self, search_args: Dict[str, Any]) -> tuple:
        """Call the secondary (Fuli) provider synchronously.

        This method runs on the comparison worker thread. It does NOT
        spawn a sub-thread; the Fuli call executes on the worker
        itself, and the Fuli bridge's per-call timeout (passed in
        ``search_args['timeout_ms']``) is responsible for capping the
        duration.

        Returns ``(raw_or_empty_str, error_category_or_None)``. Never
        raises. A None error category means the call succeeded; a
        non-None error category is a short machine-readable tag.

        **Cancellation limitation:** Python cannot forcibly terminate
        a running thread. If the Fuli bridge fails to honor its own
        timeout, this worker stays blocked on the call; the executor's
        other workers (and the foreground) are unaffected. The
        blocked worker is observed via ``comparison_jobs_orphaned``
        once it eventually returns.
        """
        secondary_call = self._secondary_call
        if secondary_call is None:
            return "", "secondary_unavailable"
        try:
            raw = secondary_call("fuli_memory_search", search_args)
        except Exception as exc:
            return "", f"secondary_error: {exc}"
        if not raw:
            return "", "secondary_empty_result"
        # Detect Fuli's error envelope. When the Fuli bridge times out
        # or fails internally, it returns a JSON object with an
        # ``error`` key. Treat this as a secondary failure so the
        # executor records secondary_status=failed.
        try:
            data = __import__("json").loads(raw)
        except Exception:
            # Non-JSON response: treat as success with raw text.
            return raw, None
        if isinstance(data, dict) and "error" in data:
            err = str(data.get("error", "unknown"))
            return "", f"secondary_error: {err}"
        return raw, None

    def _parse_secondary_results(self, raw: str) -> Optional[list]:
        if not raw:
            return None
        try:
            data = __import__("json").loads(raw)
        except Exception:
            return [raw]
        if isinstance(data, list):
            return list(data)
        if isinstance(data, dict):
            results = data.get("results")
            if isinstance(results, list):
                return list(results)
        return None

    @staticmethod
    def _detect_retrieval_mode(search_args: Dict[str, Any]) -> str:
        mode = search_args.get("mode")
        if isinstance(mode, str) and mode:
            return mode
        return "unknown"

    # --- persistence worker (single thread) ---------------------------

    def start_persistence_worker(self, store: ComparisonStore) -> None:
        """Start the single persistence worker. Idempotent.

        Implementation note: we use a plain ``threading.Thread`` with
        ``daemon=True`` rather than a ``ThreadPoolExecutor`` for the
        persistence worker. The persistence worker is an infinite
        loop; the executor's ``shutdown(wait=True)`` would block
        forever waiting for the task to complete. A daemon thread
        exits at interpreter shutdown, and our ``_stop_event``
        triggers a clean exit before that.
        """
        if self._persistence_thread is None:
            self._persistence_thread = threading.Thread(
                target=self._run_persistence,
                args=(store,),
                name="shadow-persist",
                daemon=True,
            )
            self._persistence_thread.start()

    def _run_persistence(self, store: ComparisonStore) -> None:
        """Drain the persistence queue serially. Runs until shutdown.

        The worker pops one job at a time off the persistence queue,
        writes it, and updates the accounting counters. Exceptions
        are caught and recorded as persistence failures; they never
        propagate out of the worker. The loop exits cleanly when the
        executor stops accepting AND the queue has been drained.
        """
        while True:
            # Fast exit when shutdown is requested. This is the only
            # path that can interrupt the worker promptly.
            if self._stop_event.is_set():
                return
            try:
                job = self._persistence_queue.get(timeout=0.1)
            except queue.Empty:
                with self._lock:
                    accepting = self._accepting
                if not accepting:
                    # Re-check the queue one more time, in case a job
                    # was enqueued between the empty get and the
                    # shutdown flag flip. If still empty, exit.
                    try:
                        job = self._persistence_queue.get_nowait()
                    except queue.Empty:
                        return
                else:
                    continue
            try:
                with self._lock:
                    self._persistence_started += 1
                result = store.record_comparison(job.comparison)
                with self._lock:
                    self._jobs_persisted += 1
                    outcome = result.get("outcome")
                    if outcome == "duplicate_idempotent":
                        self._duplicate_idempotent += 1
                    elif outcome == "inserted":
                        pass
                    elif outcome and "unexpected" in outcome:
                        self._unexpected_collision += 1
            except Exception as exc:
                with self._lock:
                    self._persistence_failed += 1
                    # Distinguish "unexpected collision" (same id,
                    # different payload — a real bug) from generic
                    # failures. The store increments its own
                    # ``_unexpected_collision`` class counter before
                    # raising ComparisonStoreError; we mirror that
                    # count here so operators see one signal in the
                    # executor's accounting.
                    if isinstance(exc, ComparisonStoreError) and (
                        "unexpected_id_collision" in str(exc)
                    ):
                        self._unexpected_collision += 1
                logger.warning(
                    "ComparisonExecutor persistence failed for job_id=%s "
                    "query_hash=%s: %s",
                    job.job_id,
                    job.query_hash,
                    exc,
                )

    # --- accounting + observability ------------------------------------

    def accounting(self) -> Dict[str, int]:
        """Return a snapshot of all accounting counters.

        The snapshot is taken under ``self._lock`` so the counters are
        consistent with each other and with the queue depth reads.
        """
        with self._lock:
            # Compute live queue_depth and active_jobs under the lock.
            # Definitions (must match the brief):
            #
            #   active_jobs = started - completed - timed_out - failed
            #
            # Note: cancellation events increment ``failed`` (see
            # _on_comparison_done's cancellation branch); cancelled
            # jobs are therefore counted inside ``failed`` to avoid
            # double subtraction against the other terminal counters.
            #
            #   outstanding_jobs =
            #       enqueued - completed - timed_out - failed - cancelled
            #       = enqueued - completed - timed_out - failed
            #       (because cancelled jobs are folded into failed)
            #
            #   queue_depth = max(0, outstanding_jobs - active_jobs)
            #
            # All three collapse to zero at idle (every enqueued job
            # reaches one of completed/timed_out/failed).
            active_jobs = max(
                0,
                self._jobs_started
                - self._jobs_completed
                - self._jobs_timed_out
                - self._jobs_failed,
            )
            queue_depth = max(0, self._jobs_enqueued - self._jobs_started)
            outstanding_jobs = queue_depth + active_jobs
            # admission_available = current semaphore value. The
            # semaphore does not expose its counter directly, but we
            # can derive it from admission_capacity minus permits
            # currently held. Permits held = outstanding_jobs (each
            # permits exactly one outstanding job).
            admission_capacity = self.max_queue_size + self.max_workers
            admission_available = max(0, admission_capacity - outstanding_jobs)
            # Pending = sampled - persisted (the no-final-row gap).
            return {
                "comparison_jobs_sampled": self._jobs_sampled,
                "comparison_jobs_enqueued": self._jobs_enqueued,
                "comparison_jobs_started": self._jobs_started,
                "comparison_jobs_completed": self._jobs_completed,
                "comparison_jobs_timed_out": self._jobs_timed_out,
                "comparison_jobs_failed": self._jobs_failed,
                "comparison_jobs_dropped_queue_full": self._jobs_dropped_queue_full,
                "comparison_jobs_persisted": self._jobs_persisted,
                "comparison_jobs_pending": self._jobs_sampled
                - self._jobs_persisted
                - self._persistence_failed,
                "comparison_jobs_orphaned": self._jobs_orphaned,
                "queue_depth": queue_depth,
                "active_jobs": active_jobs,
                "outstanding_jobs": outstanding_jobs,
                "queue_high_watermark": self._queue_high_watermark,
                "active_high_watermark": self._active_high_watermark,
                "outstanding_high_watermark": self._outstanding_high_watermark,
                "persistence_queue_depth": self._persistence_queue.qsize(),
                "persistence_started": self._persistence_started,
                "persistence_failed": self._persistence_failed,
                "duplicate_idempotent": self._duplicate_idempotent,
                "unexpected_collision": self._unexpected_collision,
                "admission_capacity": admission_capacity,
                "admission_available": admission_available,
            }

    def is_balanced(self) -> bool:
        """True iff the accounting is internally consistent.

        The five invariant identities:
          (1) sampled == enqueued + dropped_queue_full
              (every sampled query is either enqueued or dropped)
          (2) enqueued == started + queue_depth
              (every enqueued job is either started or still queued)
          (3) started == completed + timed_out + failed
              (every started job finishes in one of those three ways)
          (4) completed + timed_out + failed == persisted
              (every comparison has exactly one persisted terminal row)
          (5) queue_depth + active_jobs <= max_queue_size + max_workers
              (admission capacity is the upper bound on outstanding)
        """
        a = self.accounting()
        return (
            a["comparison_jobs_sampled"]
            == a["comparison_jobs_enqueued"]
            + a["comparison_jobs_dropped_queue_full"]
            and a["comparison_jobs_enqueued"]
            == a["comparison_jobs_started"] + a["queue_depth"]
            and a["comparison_jobs_started"]
            == a["comparison_jobs_completed"]
            + a["comparison_jobs_timed_out"]
            + a["comparison_jobs_failed"]
            and a["comparison_jobs_completed"]
            + a["comparison_jobs_timed_out"]
            + a["comparison_jobs_failed"]
            == a["comparison_jobs_persisted"]
            and a["outstanding_jobs"]
            <= a["admission_capacity"]
        )

    # --- flush + shutdown ---------------------------------------------

    def flush(self, timeout_seconds: float = 5.0) -> Dict[str, Any]:
        """Wait for all queued and active jobs to finish.

        Steps:
          1. Wait for all comparison futures to finish.
          2. Wait for the persistence queue to drain.
          3. Wait for the persistence worker to finish all in-flight
             writes (every started persistence has either succeeded
             or failed).

        NOTE: flush() does NOT stop accepting new jobs. Only
        shutdown() does. This makes flush() safe to call repeatedly
        (the dry-run script may flush, assert on row counts, then
        enqueue more and flush again).
        """
        deadline = time.monotonic() + max(0.1, timeout_seconds)
        with self._lock:
            futures = list(self._enqueued_futures)
        for f in futures:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                f.result(timeout=remaining)
            except Exception:
                pass
        # Drain the persistence queue AND wait for in-flight writes
        # to terminate. The worker pops one item at a time; between
        # the pop and the store.record_completion, the item is
        # "in flight" — neither in the queue nor in the count of
        # succeeded/failed. Loop until persistence_started equals
        # persistence_succeeded + persistence_failed AND the queue
        # is empty.
        while True:
            with self._lock:
                pers_started = self._persistence_started
                pers_succeeded = self._jobs_persisted
                pers_failed = self._persistence_failed
                pers_queue_depth = self._persistence_queue.qsize()
            in_flight = pers_started - pers_succeeded - pers_failed
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                break
            if pers_queue_depth == 0 and in_flight <= 0:
                break
            time.sleep(min(0.02, remaining))
        accounting = self.accounting()
        flushed = (
            accounting["comparison_jobs_pending"] == 0
            and accounting["persistence_queue_depth"] == 0
            and accounting["persistence_started"]
            == accounting["persistence_failed"]
            + accounting["comparison_jobs_persisted"]
        )
        return {
            "flushed": flushed,
            "deadline_hit": not flushed,
            "executor_accounting": accounting,
        }

    def shutdown(self, drain_timeout_seconds: float = 5.0) -> Dict[str, Any]:
        """Six-step drain. Idempotent.

        1. stop accepting new comparison jobs
        2. drain the comparison queue within the deadline
        3. drain the persistence queue within the deadline
        4. record remaining / lost jobs
        5. shut down the secondary provider (caller's responsibility)
        6. shut down the primary provider (caller's responsibility)
        """
        if self._stopped:
            return {"already_stopped": True, "executor_accounting": self.accounting()}
        with self._lock:
            self._accepting = False
        # Step 1: stop accepting new jobs (already done above).
        # Step 2: drain the comparison queue. We use
        # ``_executor.shutdown(wait=True, cancel_futures=False)`` to
        # wait for the comparison workers to finish. The workers
        # process jobs from the executor's internal queue (not the
        # user's comparison queue); each worker pushes a job to the
        # persistence queue when it finishes. With max_workers=1
        # and an unbounded internal queue, all submitted jobs will
        # run to completion before shutdown returns.
        # ``cancel_futures=False`` ensures we don't drop pending
        # comparisons.
        self._executor.shutdown(wait=True, cancel_futures=False)
        # Step 3: drain the persistence queue. flush() waits for
        # the persistence worker to consume the queue and complete
        # all in-flight writes. We do NOT set _stop_event yet
        # because the persistence worker needs to keep draining.
        flush_result = self.flush(timeout_seconds=drain_timeout_seconds)
        # Step 4: now signal the persistence worker to exit. The
        # worker checks _stop_event at the top of its loop and
        # returns promptly.
        self._stop_event.set()
        # Step 5: wait for the persistence thread to join.
        if self._persistence_thread is not None and self._persistence_thread.is_alive():
            self._persistence_thread.join(timeout=drain_timeout_seconds)
        accounting = flush_result.get("executor_accounting", self.accounting())
        lost_jobs = accounting["comparison_jobs_pending"]
        with self._lock:
            self._stopped = True
        return {
            "already_stopped": False,
            "drain_flushed": flush_result.get("flushed", False),
            "lost_jobs": lost_jobs,
            "executor_accounting": accounting,
        }


__all__ = [
    "ComparisonJob",
    "ComparisonExecutor",
]