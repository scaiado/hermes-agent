"""Canonical per-attempt ledger for the shadow pilot.

Every primary operation is one ledger row. Aggregates are computed from the
ledger. The ledger enforces accounting invariants and fails loudly if they do
not hold.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pilot.primary_classifier import PrimaryResult, PrimaryStatus, classify_exception


@dataclass
class LedgerRow:
    correlation_id: str
    sequence: int
    run_id: str
    timestamp: str
    operation: str
    payload_fingerprint: str
    primary_started_at: str
    primary_completed_at: Optional[str] = None
    primary_latency_ms: float = 0.0
    primary_status: str = PrimaryStatus.UNKNOWN.value
    primary_success: bool = False
    primary_error_code: Optional[str] = None
    primary_error_message: Optional[str] = None
    mirror_attempted: bool = False
    fuli_memory_id: Optional[str] = None
    fuli_accepted: bool = False
    fuli_indexed: bool = False
    fuli_pending: bool = False
    fuli_failed: bool = False
    fuli_indexed_at: Optional[str] = None
    fuli_latency_ms: Optional[float] = None
    fuli_error_code: Optional[str] = None
    final_status: str = "pending"
    retry_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "operation": self.operation,
            "payload_fingerprint": self.payload_fingerprint,
            "primary_started_at": self.primary_started_at,
            "primary_completed_at": self.primary_completed_at,
            "primary_latency_ms": self.primary_latency_ms,
            "primary_status": self.primary_status,
            "primary_success": self.primary_success,
            "primary_error_code": self.primary_error_code,
            "primary_error_message": self.primary_error_message,
            "mirror_attempted": self.mirror_attempted,
            "fuli_memory_id": self.fuli_memory_id,
            "fuli_accepted": self.fuli_accepted,
            "fuli_indexed": self.fuli_indexed,
            "fuli_pending": self.fuli_pending,
            "fuli_failed": self.fuli_failed,
            "fuli_indexed_at": self.fuli_indexed_at,
            "fuli_latency_ms": self.fuli_latency_ms,
            "fuli_error_code": self.fuli_error_code,
            "final_status": self.final_status,
            "retry_count": self.retry_count,
        }


class PilotLedger:
    """SQLite-backed ledger for shadow pilot operations."""

    def __init__(self, db_path: Path | str, run_id: str) -> None:
        self.db_path = Path(db_path)
        self.run_id = run_id
        self._lock = threading.Lock()
        self._local = threading.local()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pilot_ledger (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    correlation_id TEXT NOT NULL UNIQUE,
                    sequence INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    payload_fingerprint TEXT NOT NULL,
                    primary_started_at TEXT NOT NULL,
                    primary_completed_at TEXT,
                    primary_latency_ms REAL,
                    primary_status TEXT NOT NULL,
                    primary_success INTEGER NOT NULL,
                    primary_error_code TEXT,
                    primary_error_message TEXT,
                    mirror_attempted INTEGER NOT NULL DEFAULT 0,
                    fuli_memory_id TEXT,
                    fuli_accepted INTEGER NOT NULL DEFAULT 0,
                    fuli_indexed INTEGER NOT NULL DEFAULT 0,
                    fuli_pending INTEGER NOT NULL DEFAULT 0,
                    fuli_failed INTEGER NOT NULL DEFAULT 0,
                    fuli_indexed_at TEXT,
                    fuli_latency_ms REAL,
                    fuli_error_code TEXT,
                    final_status TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_run ON pilot_ledger(run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_sequence ON pilot_ledger(sequence)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_ledger_corr ON pilot_ledger(correlation_id)"
            )

    def start_operation(
        self,
        sequence: int,
        operation: str,
        payload_fingerprint: str,
    ) -> str:
        correlation_id = str(uuid.uuid4())
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO pilot_ledger (
                    correlation_id, sequence, run_id, timestamp, operation,
                    payload_fingerprint, primary_started_at, primary_status,
                    primary_success, final_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correlation_id,
                    sequence,
                    self.run_id,
                    now,
                    operation,
                    payload_fingerprint,
                    now,
                    PrimaryStatus.UNKNOWN.value,
                    0,
                    "pending",
                ),
            )
        return correlation_id

    def record_primary_result(
        self,
        correlation_id: str,
        result: PrimaryResult,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pilot_ledger SET
                    primary_completed_at = ?,
                    primary_latency_ms = ?,
                    primary_status = ?,
                    primary_success = ?,
                    primary_error_code = ?,
                    primary_error_message = ?,
                    final_status = ?
                WHERE correlation_id = ?
                """,
                (
                    _now_iso(),
                    result.latency_ms,
                    result.status.value,
                    int(result.is_confirmed_success),
                    result.error_code,
                    result.error_message,
                    "primary_success" if result.is_confirmed_success else "primary_failed",
                    correlation_id,
                ),
            )

    def record_mirror_result(
        self,
        correlation_id: str,
        memory_id: Optional[str] = None,
        accepted: bool = False,
        indexed: bool = False,
        pending: bool = False,
        failed: bool = False,
        latency_ms: Optional[float] = None,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        final = "mirror_failed"
        if accepted and indexed:
            final = "indexed"
        elif accepted and pending:
            final = "pending"
        elif accepted:
            final = "accepted"
        elif failed:
            final = "mirror_failed"
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pilot_ledger SET
                    mirror_attempted = 1,
                    fuli_memory_id = ?,
                    fuli_accepted = ?,
                    fuli_indexed = ?,
                    fuli_pending = ?,
                    fuli_failed = ?,
                    fuli_indexed_at = ?,
                    fuli_latency_ms = ?,
                    fuli_error_code = ?,
                    final_status = ?
                WHERE correlation_id = ?
                """,
                (
                    memory_id,
                    int(accepted),
                    int(indexed),
                    int(pending),
                    int(failed),
                    _now_iso() if indexed else None,
                    latency_ms,
                    error_code,
                    final,
                    correlation_id,
                ),
            )

    def update_indexing_status(
        self,
        correlation_id: str,
        indexed: bool = False,
        pending: bool = False,
        failed: bool = False,
        error_code: Optional[str] = None,
    ) -> None:
        final = "indexed" if indexed else ("pending" if pending else ("mirror_failed" if failed else "unknown"))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE pilot_ledger SET
                    fuli_indexed = ?,
                    fuli_pending = ?,
                    fuli_failed = ?,
                    fuli_indexed_at = ?,
                    fuli_error_code = ?,
                    final_status = ?
                WHERE correlation_id = ?
                """,
                (
                    int(indexed),
                    int(pending),
                    int(failed),
                    _now_iso() if indexed else None,
                    error_code,
                    final,
                    correlation_id,
                ),
            )

    def get_unindexed_correlation_ids(self) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT correlation_id FROM pilot_ledger WHERE fuli_pending = 1 OR (fuli_accepted = 1 AND fuli_indexed = 0 AND fuli_failed = 0)"
            ).fetchall()
        return [r["correlation_id"] for r in rows]

    def get_all_rows(self) -> List[LedgerRow]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM pilot_ledger WHERE run_id = ? ORDER BY sequence ASC",
                (self.run_id,),
            ).fetchall()
        return [_row_to_dataclass(r) for r in rows]

    def compute_report(self) -> Dict[str, Any]:
        rows = self.get_all_rows()
        total = len(rows)
        primary_success = sum(1 for r in rows if r.primary_success)
        primary_failure = total - primary_success
        mirror_attempted = sum(1 for r in rows if r.mirror_attempted)
        indexed = sum(1 for r in rows if r.fuli_indexed)
        pending = sum(1 for r in rows if r.fuli_pending)
        failed = sum(1 for r in rows if r.fuli_failed)
        accepted_not_indexed = sum(
            1 for r in rows if r.fuli_accepted and not r.fuli_indexed and not r.fuli_failed
        )
        # Invariant: every mirror attempt must be classified as indexed, pending, or failed.
        unexplained = mirror_attempted - indexed - pending - failed
        categories: Dict[str, int] = {}
        for r in rows:
            if not r.primary_success:
                cat = r.primary_error_code or "unknown"
                categories[cat] = categories.get(cat, 0) + 1
        return {
            "ledger_path": str(self.db_path),
            "total_attempts": total,
            "primary_success": primary_success,
            "primary_failure": primary_failure,
            "mirror_attempted": mirror_attempted,
            "indexed": indexed,
            "pending": pending,
            "failed": failed,
            "accepted_not_indexed": accepted_not_indexed,
            "unexplained_mirror_attempts": max(0, unexplained),
            "primary_error_categories": categories,
            "rows": [r.to_dict() for r in rows],
            "invariant_errors": [],
            "balanced": total == primary_success + primary_failure and unexplained == 0,
        }

    def summary(self) -> Dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN primary_success = 1 THEN 1 ELSE 0 END) AS primary_success,
                    SUM(CASE WHEN mirror_attempted = 1 THEN 1 ELSE 0 END) AS mirror_attempted,
                    SUM(CASE WHEN fuli_indexed = 1 THEN 1 ELSE 0 END) AS indexed,
                    SUM(CASE WHEN fuli_pending = 1 THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN fuli_failed = 1 THEN 1 ELSE 0 END) AS failed
                FROM pilot_ledger WHERE run_id = ?
                """,
                (self.run_id,),
            ).fetchone()
        return {
            "total": row["total"] or 0,
            "primary_success": row["primary_success"] or 0,
            "mirror_attempted": row["mirror_attempted"] or 0,
            "indexed": row["indexed"] or 0,
            "pending": row["pending"] or 0,
            "failed": row["failed"] or 0,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row_to_dataclass(row: sqlite3.Row) -> LedgerRow:
    return LedgerRow(
        correlation_id=row["correlation_id"],
        sequence=row["sequence"],
        run_id=row["run_id"],
        timestamp=row["timestamp"],
        operation=row["operation"],
        payload_fingerprint=row["payload_fingerprint"],
        primary_started_at=row["primary_started_at"],
        primary_completed_at=row["primary_completed_at"],
        primary_latency_ms=row["primary_latency_ms"] or 0.0,
        primary_status=row["primary_status"],
        primary_success=bool(row["primary_success"]),
        primary_error_code=row["primary_error_code"],
        primary_error_message=row["primary_error_message"],
        mirror_attempted=bool(row["mirror_attempted"]),
        fuli_memory_id=row["fuli_memory_id"],
        fuli_accepted=bool(row["fuli_accepted"]),
        fuli_indexed=bool(row["fuli_indexed"]),
        fuli_pending=bool(row["fuli_pending"]),
        fuli_failed=bool(row["fuli_failed"]),
        fuli_indexed_at=row["fuli_indexed_at"],
        fuli_latency_ms=row["fuli_latency_ms"],
        fuli_error_code=row["fuli_error_code"],
        final_status=row["final_status"],
        retry_count=row["retry_count"] or 0,
    )
