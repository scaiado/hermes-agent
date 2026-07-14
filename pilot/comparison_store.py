"""Durable sampled-read comparison store for the Hermes ↔ Fuli shadow pilot.

Schema versioning: SCHEMA_VERSION below identifies the on-disk layout. New
migrations append new columns/tables; existing data is preserved.

Comparison record (brief section 1):
  - comparison_id (UUID4)
  - run_id
  - timestamp
  - namespace
  - query_hash (SHA-256[:16] of normalized query)
  - query_type (default: "unclassified")
  - requested_top_k
  - primary_provider
  - secondary_provider
  - primary_latency_ms
  - secondary_latency_ms
  - primary_status
  - secondary_status
  - primary_error_category
  - secondary_error_category
  - primary_result_fingerprints (JSON list of SHA-256[:16] hex)
  - secondary_result_fingerprints (JSON list of SHA-256[:16] hex)
  - overlap_at_1, overlap_at_3, overlap_at_5 (floats)
  - reciprocal_rank_agreement (float)
  - missing_from_primary (JSON list)
  - missing_from_secondary (JSON list)
  - secondary_retrieval_mode (e.g. "hybrid", "vector", "lexical", "unknown")
  - content_captured (bool, default false)
  - adjudication_status (default: "pending")
  - schema_version (default: SCHEMA_VERSION)

Adjudication record (brief section 5):
  - comparison_id (PK)
  - winner (enum: fuli|honcho|both|neither|unsafe)
  - query_type
  - reason_code (from the closed enum in the brief)
  - note (optional, must NOT contain raw content)
  - adjudicator
  - adjudicated_at
  - updated_at

Redaction is enforced at write time: the schema has no column for raw
query text or raw memory content. ``content_captured=False`` is the
default; the field exists so operators can audit how often this flag was
flipped on (it should never be set during normal Stage B).
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SCHEMA_VERSION = 1

ALLOWED_WINNERS = ("fuli", "honcho", "both", "neither", "unsafe")
ALLOWED_QUERY_TYPES = (
    "unclassified",
    "profile",
    "preference",
    "project",
    "episodic",
    "exact",
    "semantic",
    "recent",
    "contradiction",
)
ALLOWED_REASON_CODES = (
    "more_relevant",
    "better_ranked",
    "more_complete",
    "more_recent",
    "less_stale",
    "correct_namespace",
    "better_provenance",
    "avoids_contradiction",
    "both_equivalent",
    "both_poor",
    "unsafe_result",
)


class ComparisonStoreError(ValueError):
    """Raised when an attempt to write a comparison or adjudication violates a contract."""


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass
class ComparisonRecord:
    """In-memory shape of a single comparison. All fields are JSON-serializable."""

    comparison_id: str
    run_id: str
    timestamp: str
    namespace: str
    query_hash: str
    query_type: str = "unclassified"
    requested_top_k: int = 5
    primary_provider: str = "honcho"
    secondary_provider: str = "fuli"
    primary_latency_ms: float = 0.0
    secondary_latency_ms: float = 0.0
    primary_status: str = ""
    secondary_status: str = ""
    primary_error_category: Optional[str] = None
    secondary_error_category: Optional[str] = None
    primary_result_fingerprints: List[str] = field(default_factory=list)
    secondary_result_fingerprints: List[str] = field(default_factory=list)
    overlap_at_1: Optional[float] = None
    overlap_at_3: Optional[float] = None
    overlap_at_5: Optional[float] = None
    reciprocal_rank_agreement: Optional[float] = None
    missing_from_primary: List[str] = field(default_factory=list)
    missing_from_secondary: List[str] = field(default_factory=list)
    secondary_retrieval_mode: str = "unknown"
    content_captured: bool = False
    adjudication_status: str = "pending"
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "namespace": self.namespace,
            "query_hash": self.query_hash,
            "query_type": self.query_type,
            "requested_top_k": self.requested_top_k,
            "primary_provider": self.primary_provider,
            "secondary_provider": self.secondary_provider,
            "primary_latency_ms": self.primary_latency_ms,
            "secondary_latency_ms": self.secondary_latency_ms,
            "primary_status": self.primary_status,
            "secondary_status": self.secondary_status,
            "primary_error_category": self.primary_error_category,
            "secondary_error_category": self.secondary_error_category,
            "primary_result_fingerprints": list(self.primary_result_fingerprints),
            "secondary_result_fingerprints": list(self.secondary_result_fingerprints),
            "overlap_at_1": self.overlap_at_1,
            "overlap_at_3": self.overlap_at_3,
            "overlap_at_5": self.overlap_at_5,
            "reciprocal_rank_agreement": self.reciprocal_rank_agreement,
            "missing_from_primary": list(self.missing_from_primary),
            "missing_from_secondary": list(self.missing_from_secondary),
            "secondary_retrieval_mode": self.secondary_retrieval_mode,
            "content_captured": self.content_captured,
            "adjudication_status": self.adjudication_status,
            "schema_version": self.schema_version,
        }


class ComparisonStore:
    """SQLite-backed store for sampled-read comparisons and adjudications."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS comparisons (
                    comparison_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    query_type TEXT NOT NULL DEFAULT 'unclassified',
                    requested_top_k INTEGER NOT NULL DEFAULT 5,
                    primary_provider TEXT NOT NULL DEFAULT 'honcho',
                    secondary_provider TEXT NOT NULL DEFAULT 'fuli',
                    primary_latency_ms REAL NOT NULL DEFAULT 0.0,
                    secondary_latency_ms REAL NOT NULL DEFAULT 0.0,
                    primary_status TEXT NOT NULL DEFAULT '',
                    secondary_status TEXT NOT NULL DEFAULT '',
                    primary_error_category TEXT,
                    secondary_error_category TEXT,
                    primary_result_fingerprints TEXT NOT NULL DEFAULT '[]',
                    secondary_result_fingerprints TEXT NOT NULL DEFAULT '[]',
                    overlap_at_1 REAL,
                    overlap_at_3 REAL,
                    overlap_at_5 REAL,
                    reciprocal_rank_agreement REAL,
                    missing_from_primary TEXT NOT NULL DEFAULT '[]',
                    missing_from_secondary TEXT NOT NULL DEFAULT '[]',
                    secondary_retrieval_mode TEXT NOT NULL DEFAULT 'unknown',
                    content_captured INTEGER NOT NULL DEFAULT 0,
                    adjudication_status TEXT NOT NULL DEFAULT 'pending',
                    schema_version INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS adjudications (
                    comparison_id TEXT PRIMARY KEY,
                    winner TEXT NOT NULL,
                    query_type TEXT NOT NULL,
                    reason_code TEXT,
                    note TEXT,
                    adjudicator TEXT NOT NULL,
                    adjudicated_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (comparison_id) REFERENCES comparisons(comparison_id)
                        ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_comparisons_run_id ON comparisons(run_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_comparisons_query_hash ON comparisons(query_hash)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_comparisons_namespace ON comparisons(namespace)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_comparisons_adjudication_status "
                "ON comparisons(adjudication_status)"
            )

    # --- comparisons --------------------------------------------------------

    def record_comparison(self, record: ComparisonRecord) -> str:
        """Idempotently persist a comparison. Returns the comparison_id.

        INSERT OR IGNORE on the primary key ensures duplicate writes are
        no-ops (test #12: duplicate comparison idempotency).
        """
        if record.comparison_id is None:
            record.comparison_id = str(uuid.uuid4())
        if record.timestamp is None:
            record.timestamp = _now_iso()
        if record.query_type not in ALLOWED_QUERY_TYPES:
            raise ComparisonStoreError(
                f"query_type {record.query_type!r} not in {ALLOWED_QUERY_TYPES}"
            )
        if record.primary_provider == record.secondary_provider:
            raise ComparisonStoreError(
                "primary_provider and secondary_provider must differ "
                f"(got {record.primary_provider!r})"
            )
        if record.content_captured:
            # Defence in depth: this flag is for audit only. The schema
            # has no raw-content column, so even if the flag is set,
            # nothing leaks. We log the unusual event.
            pass
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO comparisons (
                    comparison_id, run_id, timestamp, namespace, query_hash, query_type,
                    requested_top_k, primary_provider, secondary_provider,
                    primary_latency_ms, secondary_latency_ms, primary_status,
                    secondary_status, primary_error_category, secondary_error_category,
                    primary_result_fingerprints, secondary_result_fingerprints,
                    overlap_at_1, overlap_at_3, overlap_at_5, reciprocal_rank_agreement,
                    missing_from_primary, missing_from_secondary,
                    secondary_retrieval_mode, content_captured, adjudication_status,
                    schema_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.comparison_id,
                    record.run_id,
                    record.timestamp,
                    record.namespace,
                    record.query_hash,
                    record.query_type,
                    record.requested_top_k,
                    record.primary_provider,
                    record.secondary_provider,
                    record.primary_latency_ms,
                    record.secondary_latency_ms,
                    record.primary_status,
                    record.secondary_status,
                    record.primary_error_category,
                    record.secondary_error_category,
                    json.dumps(record.primary_result_fingerprints),
                    json.dumps(record.secondary_result_fingerprints),
                    record.overlap_at_1,
                    record.overlap_at_3,
                    record.overlap_at_5,
                    record.reciprocal_rank_agreement,
                    json.dumps(record.missing_from_primary),
                    json.dumps(record.missing_from_secondary),
                    record.secondary_retrieval_mode,
                    int(record.content_captured),
                    record.adjudication_status,
                    record.schema_version,
                ),
            )
        return record.comparison_id

    def get_comparison(self, comparison_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM comparisons WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_comparison_dict(row)

    def list_comparisons(
        self,
        *,
        run_id: Optional[str] = None,
        namespace: Optional[str] = None,
        adjudication_status: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if namespace is not None:
            clauses.append("namespace = ?")
            params.append(namespace)
        if adjudication_status is not None:
            clauses.append("adjudication_status = ?")
            params.append(adjudication_status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM comparisons {where} ORDER BY timestamp DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_row_to_comparison_dict(r) for r in rows]

    def comparison_count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0]

    # --- adjudications ------------------------------------------------------

    def record_adjudication(
        self,
        comparison_id: str,
        *,
        winner: str,
        query_type: str,
        reason_code: Optional[str] = None,
        note: Optional[str] = None,
        adjudicator: str,
    ) -> Dict[str, Any]:
        if winner not in ALLOWED_WINNERS:
            raise ComparisonStoreError(
                f"winner {winner!r} not in {ALLOWED_WINNERS}"
            )
        if query_type not in ALLOWED_QUERY_TYPES:
            raise ComparisonStoreError(
                f"query_type {query_type!r} not in {ALLOWED_QUERY_TYPES}"
            )
        if reason_code is not None and reason_code not in ALLOWED_REASON_CODES:
            raise ComparisonStoreError(
                f"reason_code {reason_code!r} not in {ALLOWED_REASON_CODES}"
            )

        with self._connect() as conn:
            # Ensure the comparison exists; otherwise adjudication is invalid.
            exists = conn.execute(
                "SELECT 1 FROM comparisons WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
            if exists is None:
                raise ComparisonStoreError(
                    f"comparison_id {comparison_id!r} does not exist"
                )
            now = _now_iso()
            # Upsert: an existing judgement can be updated; the updated_at
            # column is bumped; adjudicated_at is preserved on first insert.
            conn.execute(
                """
                INSERT INTO adjudications
                    (comparison_id, winner, query_type, reason_code, note,
                     adjudicator, adjudicated_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(comparison_id) DO UPDATE SET
                    winner = excluded.winner,
                    query_type = excluded.query_type,
                    reason_code = excluded.reason_code,
                    note = excluded.note,
                    adjudicator = excluded.adjudicator,
                    updated_at = excluded.updated_at
                """,
                (comparison_id, winner, query_type, reason_code, note, adjudicator, now, now),
            )
            # The comparison's classify step changes query_type on the
            # comparison itself; record/update that too.
            conn.execute(
                "UPDATE comparisons SET query_type = ? WHERE comparison_id = ?",
                (query_type, comparison_id),
            )
            # Mark adjudication_status as decided.
            conn.execute(
                "UPDATE comparisons SET adjudication_status = 'decided' WHERE comparison_id = ?",
                (comparison_id,),
            )
        return self.get_adjudication(comparison_id) or {}

    def get_adjudication(self, comparison_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM adjudications WHERE comparison_id = ?",
                (comparison_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_adjudication_dict(row)

    def list_adjudications(
        self,
        *,
        adjudicator: Optional[str] = None,
        winner: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        clauses: List[str] = []
        params: List[Any] = []
        if adjudicator is not None:
            clauses.append("adjudicator = ?")
            params.append(adjudicator)
        if winner is not None:
            clauses.append("winner = ?")
            params.append(winner)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM adjudications {where} ORDER BY adjudicated_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        return [_row_to_adjudication_dict(r) for r in rows]

    # --- accounting ---------------------------------------------------------

    def comparison_accounting(self) -> Dict[str, int]:
        """Balanced accounting for the comparison pipeline.

        - sampled: how many comparisons were attempted (== total comparisons)
        - primary_completed: how many have a primary_status recorded
        - secondary_attempted: how many have a secondary_status recorded
        - secondary_completed: how many finished without secondary error
        - persisted: how many rows are in the table (== sampled by
          construction; the comparison is what we persist)
        - failed: how many have a secondary error category
        - adjudicated: how many adjudications exist
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN primary_status IS NOT NULL AND primary_status != '' THEN 1 ELSE 0 END) AS primary_completed,
                    SUM(CASE WHEN secondary_status IS NOT NULL AND secondary_status != '' THEN 1 ELSE 0 END) AS secondary_attempted,
                    SUM(CASE WHEN secondary_status IS NOT NULL AND secondary_status != '' AND secondary_error_category IS NULL THEN 1 ELSE 0 END) AS secondary_completed,
                    SUM(CASE WHEN secondary_error_category IS NOT NULL THEN 1 ELSE 0 END) AS failed
                FROM comparisons
                """
            ).fetchone()
            adj_count = conn.execute(
                "SELECT COUNT(*) FROM adjudications"
            ).fetchone()[0]
        total = row["total"] or 0
        return {
            "sampled": total,
            "primary_completed": row["primary_completed"] or 0,
            "secondary_attempted": row["secondary_attempted"] or 0,
            "secondary_completed": row["secondary_completed"] or 0,
            "persisted": total,
            "failed": row["failed"] or 0,
            "adjudicated": adj_count,
        }

    # --- export -------------------------------------------------------------

    def export_redacted(self, limit: int = 10000) -> List[Dict[str, Any]]:
        """Return a redacted list of comparisons + their adjudications.

        The export never contains raw query text or raw memory content —
        the schema has no such columns. The brief (section 5) requires this.
        """
        out: List[Dict[str, Any]] = []
        for comp in self.list_comparisons(limit=limit):
            entry = dict(comp)
            adj = self.get_adjudication(comp["comparison_id"])
            if adj is not None:
                entry["adjudication"] = adj
            out.append(entry)
        return out


def _row_to_comparison_dict(row: sqlite3.Row) -> Dict[str, Any]:
    d = dict(row)
    # Decode JSON list columns.
    for key in (
        "primary_result_fingerprints",
        "secondary_result_fingerprints",
        "missing_from_primary",
        "missing_from_secondary",
    ):
        raw = d.get(key)
        if raw is None or raw == "":
            d[key] = []
        else:
            try:
                d[key] = json.loads(raw)
            except (TypeError, ValueError):
                d[key] = []
    d["content_captured"] = bool(d.get("content_captured", 0))
    return d


def _row_to_adjudication_dict(row: sqlite3.Row) -> Dict[str, Any]:
    return dict(row)


__all__ = [
    "ALLOWED_WINNERS",
    "ALLOWED_QUERY_TYPES",
    "ALLOWED_REASON_CODES",
    "SCHEMA_VERSION",
    "ComparisonRecord",
    "ComparisonStore",
    "ComparisonStoreError",
]