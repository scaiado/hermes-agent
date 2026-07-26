"""Shadow evidence store for Hermes memory provider comparison.

A lightweight local SQLite store used by the shadow provider. Keeps two tables:
  - mirrored_writes: outcomes of write-mirroring attempts
  - observations: read-comparison samples between primary and secondary providers

No full content is stored unless capture_content is enabled. Fingerprints are
SHA-256 hex digests of canonical content strings so the store stays small and
private by default.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


class ShadowEvidenceStore:
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
                CREATE TABLE IF NOT EXISTS mirrored_writes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    primary_success INTEGER NOT NULL,
                    shadow_success INTEGER NOT NULL,
                    shadow_error TEXT,
                    latency_ms REAL NOT NULL,
                    content_fingerprint TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS observations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    query_hash TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    primary_latency_ms REAL NOT NULL,
                    shadow_latency_ms REAL NOT NULL,
                    primary_fingerprints TEXT NOT NULL,
                    shadow_fingerprints TEXT NOT NULL,
                    overlap_at_1 REAL,
                    overlap_at_3 REAL,
                    overlap_at_5 REAL,
                    missing_primary_fingerprints TEXT NOT NULL,
                    error_category TEXT,
                    shadow_mode TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS diagnostic_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    operation TEXT,
                    event_type TEXT NOT NULL
                )
                """
            )

    def _migrate_add_columns(self) -> None:
        """Add columns added after the initial schema without dropping tables."""
        with self._connect() as conn:
            existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(mirrored_writes)")}
            if "correlation_id" not in existing_cols:
                conn.execute("ALTER TABLE mirrored_writes ADD COLUMN correlation_id TEXT")
            if "fuli_memory_id" not in existing_cols:
                conn.execute("ALTER TABLE mirrored_writes ADD COLUMN fuli_memory_id TEXT")
            if "metadata" not in existing_cols:
                conn.execute("ALTER TABLE mirrored_writes ADD COLUMN metadata TEXT")
            existing_diag = {row[1] for row in conn.execute("PRAGMA table_info(diagnostic_events)")}
            if "correlation_id" not in existing_diag:
                conn.execute("ALTER TABLE diagnostic_events ADD COLUMN correlation_id TEXT")
            if "primary_status" not in existing_diag:
                conn.execute("ALTER TABLE diagnostic_events ADD COLUMN primary_status TEXT")
            if "error_message" not in existing_diag:
                conn.execute("ALTER TABLE diagnostic_events ADD COLUMN error_message TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mirrored_writes_corr ON mirrored_writes(correlation_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_mirrored_writes_ns ON mirrored_writes(namespace)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_diagnostic_events_corr ON diagnostic_events(correlation_id)"
            )

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._migrate_add_columns()

    @staticmethod
    def fingerprint(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def query_hash(query: str) -> str:
        return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]

    def record_mirrored_write(
        self,
        namespace: str,
        operation: str,
        primary_success: bool,
        shadow_success: bool,
        shadow_error: Optional[str],
        latency_ms: float,
        content_fingerprint: Optional[str] = None,
        correlation_id: Optional[str] = None,
        fuli_memory_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO mirrored_writes
                (timestamp, namespace, operation, primary_success, shadow_success,
                 shadow_error, latency_ms, content_fingerprint, correlation_id, fuli_memory_id, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._now(),
                    namespace,
                    operation,
                    int(primary_success),
                    int(shadow_success),
                    shadow_error,
                    latency_ms,
                    content_fingerprint,
                    correlation_id,
                    fuli_memory_id,
                    json.dumps(metadata or {}),
                ),
            )

    def record_diagnostic_event(
        self,
        namespace: str,
        operation: str,
        event_type: str,
        correlation_id: Optional[str] = None,
        primary_status: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO diagnostic_events
                (timestamp, namespace, operation, event_type, correlation_id, primary_status, error_message)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._now(),
                    namespace,
                    operation,
                    event_type,
                    correlation_id,
                    primary_status,
                    error_message,
                ),
            )

    def query_by_correlation(self, correlation_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM mirrored_writes WHERE correlation_id = ? LIMIT 1",
                (correlation_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def diagnostic_summary(self) -> Dict[str, Any]:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM diagnostic_events").fetchone()[0]
            by_type = {
                row["event_type"]: row["cnt"]
                for row in conn.execute(
                    "SELECT event_type, COUNT(*) AS cnt FROM diagnostic_events GROUP BY event_type"
                )
            }
        return {"total": total, "by_type": by_type}

    def record_observation(
        self,
        query_hash: str,
        namespace: str,
        primary_latency_ms: float,
        shadow_latency_ms: float,
        primary_fingerprints: List[str],
        shadow_fingerprints: List[str],
        overlap_at_1: Optional[float],
        overlap_at_3: Optional[float],
        overlap_at_5: Optional[float],
        missing_primary_fingerprints: List[str],
        error_category: Optional[str],
        shadow_mode: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO observations
                (timestamp, query_hash, namespace, primary_latency_ms, shadow_latency_ms,
                 primary_fingerprints, shadow_fingerprints, overlap_at_1, overlap_at_3,
                 overlap_at_5, missing_primary_fingerprints, error_category, shadow_mode)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._now(),
                    query_hash,
                    namespace,
                    primary_latency_ms,
                    shadow_latency_ms,
                    json.dumps(primary_fingerprints),
                    json.dumps(shadow_fingerprints),
                    overlap_at_1,
                    overlap_at_3,
                    overlap_at_5,
                    json.dumps(missing_primary_fingerprints),
                    error_category,
                    shadow_mode,
                ),
            )

    def report(self) -> Dict[str, Any]:
        with self._connect() as conn:
            write_rows = conn.execute(
                """
                SELECT
                    COUNT(*) AS attempted,
                    SUM(CASE WHEN primary_success = 1 THEN 1 ELSE 0 END) AS primary_success,
                    SUM(CASE WHEN shadow_success = 1 THEN 1 ELSE 0 END) AS shadow_success,
                    SUM(CASE WHEN shadow_success = 0 AND primary_success = 1 THEN 1 ELSE 0 END) AS shadow_failures,
                    AVG(latency_ms) AS avg_latency_ms
                FROM mirrored_writes
                """
            ).fetchone()
            obs_rows = conn.execute(
                """
                SELECT
                    COUNT(*) AS sample_count,
                    AVG(primary_latency_ms) AS avg_primary_latency_ms,
                    AVG(shadow_latency_ms) AS avg_shadow_latency_ms,
                    AVG(overlap_at_1) AS overlap_at_1,
                    AVG(overlap_at_3) AS overlap_at_3,
                    AVG(overlap_at_5) AS overlap_at_5,
                    SUM(CASE WHEN error_category IS NOT NULL THEN 1 ELSE 0 END) AS error_count
                FROM observations
                """
            ).fetchone()
            namespaces = {
                row["namespace"]
                for row in conn.execute("SELECT DISTINCT namespace FROM mirrored_writes")
            } | {
                row["namespace"]
                for row in conn.execute("SELECT DISTINCT namespace FROM observations")
            }
        return {
            "writes": {
                "attempted": write_rows["attempted"] or 0,
                "primary_success": write_rows["primary_success"] or 0,
                "shadow_success": write_rows["shadow_success"] or 0,
                "shadow_failure_rate": (
                    (write_rows["shadow_failures"] or 0) / max(write_rows["attempted"] or 0, 1)
                ),
                "average_latency_ms": write_rows["avg_latency_ms"] or 0.0,
            },
            "reads": {
                "sample_count": obs_rows["sample_count"] or 0,
                "average_primary_latency_ms": obs_rows["avg_primary_latency_ms"] or 0.0,
                "average_shadow_latency_ms": obs_rows["avg_shadow_latency_ms"] or 0.0,
                "overlap_at_1": obs_rows["overlap_at_1"],
                "overlap_at_3": obs_rows["overlap_at_3"],
                "overlap_at_5": obs_rows["overlap_at_5"],
                "error_rate": (obs_rows["error_count"] or 0) / max(obs_rows["sample_count"] or 0, 1),
            },
            "namespaces": sorted(namespaces),
            "diagnostics": self.diagnostic_summary(),
        }

    def purge_synthetic(self, namespace: Optional[str] = None, run_id: Optional[str] = None) -> int:
        """Remove synthetic mirrored writes and observations by namespace/run metadata.

        Does not touch actual memory backends (Honcho/Fuli); only the evidence store.
        """
        with self._connect() as conn:
            if namespace:
                conn.execute("DELETE FROM mirrored_writes WHERE namespace = ?", (namespace,))
                conn.execute("DELETE FROM observations WHERE namespace = ?", (namespace,))
                conn.execute("DELETE FROM diagnostic_events WHERE namespace = ?", (namespace,))
            else:
                conn.execute("DELETE FROM mirrored_writes")
                conn.execute("DELETE FROM observations")
                conn.execute("DELETE FROM diagnostic_events")
        return 0

    def purge_comparisons(self, before_timestamp: Optional[str] = None) -> int:
        with self._connect() as conn:
            if before_timestamp is None:
                before_timestamp = self._now()
            cursor = conn.execute(
                "DELETE FROM observations WHERE timestamp < ?",
                (before_timestamp,),
            )
            return cursor.rowcount

    def _now(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
