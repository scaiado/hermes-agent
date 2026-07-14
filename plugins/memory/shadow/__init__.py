"""Shadow memory provider — Honcho primary, Fuli secondary.

This provider lets Hermes keep Honcho as the authoritative memory source while
mirroring writes to and sampling reads from Fuli for comparison. Fuli never
affects the live answers returned to the model.

Config shape (under ``memory.shadow``):
  primary_provider: honcho
  secondary_provider: fuli
  enabled: false
  mirror_writes: false
  compare_reads: false
  sample_rate: 0.0
  sampling_seed: 0            # integer; same query + seed => same sample decision
  comparison_budget_ms: 250  # hard wall-clock cap on per-comparison work
  timeout_ms: 250
  capture_content: false
  namespace: hermes:default

When ``enabled`` is false, the shadow provider behaves exactly like the
primary provider alone (no Fuli tools, no extra work).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from pilot.primary_classifier import PrimaryResult, classify_honcho_result
from pilot.comparison_metrics import (
    missing_from,
    overlap_at_k,
    reciprocal_rank_agreement,
    result_fingerprints,
)
from pilot.comparison_store import ComparisonRecord, ComparisonStore
from pilot.sampling import SamplingDecision, query_hash_for, should_sample
from plugins.memory import load_memory_provider
from plugins.memory.shadow.shadow_store import ShadowEvidenceStore

logger = logging.getLogger(__name__)

WRITE_TOOLS = {"honcho_profile", "honcho_conclude"}
READ_TOOLS = {"honcho_search", "honcho_context", "honcho_reasoning"}


class ShadowMemoryProvider(MemoryProvider):
    """Wraps a primary MemoryProvider and a secondary MemoryProvider.

    The primary provider's tool schemas and behavior are preserved. Fuli is used
    only for best-effort mirroring and comparison; its results are recorded but
    never returned to the agent.
    """

    def __init__(self) -> None:
        self._primary: Optional[MemoryProvider] = None
        self._secondary: Optional[MemoryProvider] = None
        self._primary_name: str = "honcho"
        self._secondary_name: str = "fuli"
        self._enabled: bool = False
        self._mirror_writes: bool = False
        self._compare_reads: bool = False
        self._sample_rate: float = 0.0
        # Deterministic sampling: same (seed, query, namespace) -> same decision.
        self._sampling_seed: int = 0
        # Hard wall-clock cap on a single comparison's secondary work.
        self._comparison_budget_ms: int = 250
        # Legacy single timeout (fallback), then operation-specific overrides.
        self._timeout_ms: int = 250
        self._write_timeout_ms: int = 10000
        self._read_timeout_ms: int = 250
        self._capture_content: bool = False
        self._namespace: str = "hermes:default"
        self._hermes_home: Optional[str] = None
        self._store: Optional[ShadowEvidenceStore] = None
        self._comparison_store: Optional[ComparisonStore] = None
        self._init_kwargs: Dict[str, Any] = {}
        # Run id the shadow provider attributes comparisons to. Defaults to
        # the session id until the caller overrides it.
        self._comparison_run_id: str = "shadow-default"

    @property
    def name(self) -> str:
        return "shadow"

    def is_available(self) -> bool:
        # Shadow is available if both primary and secondary providers can be loaded.
        try:
            primary = self._load_provider(self._primary_name)
            secondary = self._load_provider(self._secondary_name)
            return primary is not None and secondary is not None
        except Exception as exc:
            logger.debug("Shadow provider not available: %s", exc)
            return False

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {"key": "primary_provider", "description": "Primary (authoritative) memory provider.", "required": False},
            {"key": "secondary_provider", "description": "Secondary (shadow) memory provider.", "required": False},
            {"key": "enabled", "description": "Enable shadow mirroring and comparison.", "required": False},
            {"key": "mirror_writes", "description": "Mirror write operations to the secondary provider.", "required": False},
            {"key": "compare_reads", "description": "Compare read results between providers.", "required": False},
            {"key": "sample_rate", "description": "Fraction of reads to compare (0.0-1.0).", "required": False},
            {"key": "sampling_seed", "description": "Integer seed for deterministic sampling (default 0).", "required": False},
            {"key": "comparison_budget_ms", "description": "Hard wall-clock cap on a single comparison's secondary work (default 250).", "required": False},
            {"key": "timeout_ms", "description": "Legacy secondary timeout in milliseconds (fallback).", "required": False},
            {"key": "write_timeout_ms", "description": "Secondary write timeout in milliseconds (default: 10000).", "required": False},
            {"key": "read_timeout_ms", "description": "Secondary read/search timeout in milliseconds (default: 250).", "required": False},
            {"key": "capture_content", "description": "Store full content in evidence (default false). NEVER enable in production.", "required": False},
            {"key": "namespace", "description": "Namespace for shadow memories.", "required": False},
        ]

    def _load_config(self) -> Dict[str, Any]:
        from hermes_constants import get_hermes_home
        from hermes_cli.config import load_config, cfg_get

        try:
            config = load_config()
        except Exception:
            config = {}
        shadow = cfg_get(config, "memory", "shadow", default={}) or {}
        if not isinstance(shadow, dict):
            shadow = {}
        # Local JSON overrides if the desktop UI wrote directly to
        # <hermes_home>/shadow/config.json.
        hermes_home = self._hermes_home or str(get_hermes_home())
        local_json = Path(hermes_home) / "shadow" / "config.json"
        if local_json.exists():
            try:
                local = json.loads(local_json.read_text())
                if isinstance(local, dict):
                    shadow.update(local)
            except Exception:
                pass
        return shadow

    def _apply_config(self, shadow: Dict[str, Any]) -> None:
        self._primary_name = str(shadow.get("primary_provider", self._primary_name))
        self._secondary_name = str(shadow.get("secondary_provider", self._secondary_name))
        self._enabled = bool(shadow.get("enabled", False))
        self._mirror_writes = bool(shadow.get("mirror_writes", False))
        self._compare_reads = bool(shadow.get("compare_reads", False))
        try:
            self._sample_rate = float(shadow.get("sample_rate", 0.0))
        except (TypeError, ValueError):
            self._sample_rate = 0.0
        self._sample_rate = max(0.0, min(1.0, self._sample_rate))
        try:
            self._sampling_seed = int(shadow.get("sampling_seed", 0))
        except (TypeError, ValueError):
            self._sampling_seed = 0
        try:
            self._comparison_budget_ms = int(shadow.get("comparison_budget_ms", 250))
        except (TypeError, ValueError):
            self._comparison_budget_ms = 250
        self._comparison_budget_ms = max(50, self._comparison_budget_ms)
        try:
            self._timeout_ms = int(shadow.get("timeout_ms", self._timeout_ms))
        except (TypeError, ValueError):
            self._timeout_ms = 250
        try:
            self._write_timeout_ms = int(shadow.get("write_timeout_ms", self._timeout_ms))
        except (TypeError, ValueError):
            self._write_timeout_ms = self._timeout_ms
        try:
            self._read_timeout_ms = int(shadow.get("read_timeout_ms", self._timeout_ms))
        except (TypeError, ValueError):
            self._read_timeout_ms = self._timeout_ms
        self._timeout_ms = max(100, self._timeout_ms)
        self._write_timeout_ms = max(100, self._write_timeout_ms)
        self._read_timeout_ms = max(100, self._read_timeout_ms)
        self._capture_content = bool(shadow.get("capture_content", False))
        self._namespace = str(shadow.get("namespace", self._namespace))

    def _load_provider(self, name: str) -> Optional[MemoryProvider]:
        if name == "shadow":
            raise ValueError("Shadow provider cannot recursively load itself")
        return load_memory_provider(name)

    def _configure_secondary(self) -> None:
        """Ensure the secondary provider has a strict timeout and namespace.

        Writes a provider JSON config for Fuli with operation-specific timeouts.
        The shadow provider's own per-call timeout args are the source of truth,
        but this config lets Fuli internals (e.g., warm-up) default correctly.
        """
        if self._secondary_name != "fuli" or not self._hermes_home:
            return
        from utils import atomic_json_write
        cfg_path = Path(self._hermes_home) / "fuli" / "config.json"
        existing: Dict[str, Any] = {}
        if cfg_path.exists():
            try:
                existing = json.loads(cfg_path.read_text())
            except Exception:
                pass
        existing.setdefault("namespace", self._namespace)
        # Write operation-specific timeouts if configured; otherwise preserve existing.
        existing.setdefault("write_timeout_ms", self._write_timeout_ms)
        existing.setdefault("read_timeout_ms", self._read_timeout_ms)
        existing.setdefault("lazy_init", True)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(cfg_path, existing, mode=0o600)

    def _should_sample(self, query_hash: str) -> SamplingDecision:
        """Deterministic seed-based sampling decision.

        Tests assert the same (seed, query_hash, namespace) always yields the
        same decision. Different seeds shift the hash bucket and therefore
        the selected sample set.
        """
        return should_sample(
            sample_rate=self._sample_rate,
            sampling_seed=self._sampling_seed,
            query_hash=query_hash,
            namespace=self._namespace,
        )

    def _create_store(self, db_path: str) -> ShadowEvidenceStore:
        return ShadowEvidenceStore(Path(db_path))

    def initialize(self, session_id: str, **kwargs) -> None:
        self._init_kwargs = dict(kwargs)
        self._hermes_home = kwargs.get("hermes_home", "")
        if not self._hermes_home:
            from hermes_constants import get_hermes_home
            self._hermes_home = str(get_hermes_home())

        self._apply_config(self._load_config())
        self._store = self._create_store(str(Path(self._hermes_home) / "memories" / "shadow.db"))
        # Comparison store lives alongside the legacy evidence store. The
        # schema is independent (no raw-content column), so even a corrupted
        # legacy store cannot leak into P1 comparisons.
        self._comparison_store = ComparisonStore(
            Path(self._hermes_home) / "memories" / "comparisons.db"
        )
        # Pin the comparison run_id to the session id so every comparison
        # in one Hermes session can be correlated. A real pilot override
        # can replace this via kwargs['comparison_run_id'].
        self._comparison_run_id = str(
            kwargs.get("comparison_run_id") or session_id or "shadow-default"
        )

        # Load primary. If already injected (tests), skip discovery.
        if self._primary is None:
            try:
                self._primary = self._load_provider(self._primary_name)
            except Exception as exc:
                logger.warning("Shadow primary provider '%s' failed to load: %s", self._primary_name, exc)
                self._primary = None

        if self._primary is None and self._primary_name != "honcho":
            logger.warning("Shadow falling back to primary 'honcho'")
            self._primary_name = "honcho"
            self._primary = self._load_provider("honcho")

        # Load secondary only if enabled.
        if self._enabled and (self._mirror_writes or self._compare_reads) and self._secondary is None:
            try:
                self._configure_secondary()
                self._secondary = self._load_provider(self._secondary_name)
            except Exception as exc:
                logger.warning("Shadow secondary provider '%s' failed to load: %s", self._secondary_name, exc)
                self._secondary = None

        if self._primary is not None:
            try:
                self._primary.initialize(session_id, **kwargs)
            except Exception as exc:
                logger.warning("Shadow primary initialize failed: %s", exc)
                self._primary = None

        if self._secondary is not None:
            try:
                self._secondary.initialize(session_id, **kwargs)
            except Exception as exc:
                logger.warning("Shadow secondary initialize failed: %s", exc)
                self._secondary = None

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        if self._primary is None:
            return []
        return self._primary.get_tool_schemas()

    def system_prompt_block(self) -> str:
        if self._primary is None:
            return ""
        return self._primary.system_prompt_block()

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if self._primary is None:
            return json.dumps({"error": "Shadow primary provider unavailable"})

        start = time.monotonic()
        primary_result = self._primary.handle_tool_call(tool_name, args, **kwargs)
        primary_latency_ms = (time.monotonic() - start) * 1000.0

        if not self._enabled or self._secondary is None:
            return primary_result

        # Classify the primary result using structured status first, then decide
        # whether mirroring is appropriate.
        primary_class = classify_honcho_result(primary_result, latency_ms=primary_latency_ms)
        pilot_meta = kwargs.get("pilot_meta") or {}
        correlation_id = pilot_meta.get("correlation_id")

        try:
            if tool_name in WRITE_TOOLS and self._mirror_writes:
                self._mirror_write(
                    tool_name, args, primary_class, correlation_id=correlation_id, pilot_meta=pilot_meta
                )
            elif tool_name in READ_TOOLS and self._compare_reads:
                # Deterministic sampling: same query => same decision.
                query_text = args.get("query", "") or ""
                q_hash = query_hash_for(query_text)
                decision = self._should_sample(q_hash)
                if decision.sample:
                    # Hard safety: comparison work happens AFTER primary
                    # returned. It is bounded by self._comparison_budget_ms
                    # and cannot fail the user request.
                    self._compare_read(
                        tool_name,
                        args,
                        primary_result,
                        primary_latency_ms,
                        query_hash=q_hash,
                        decision=decision,
                    )
        except Exception as exc:
            logger.debug("Shadow secondary work failed for %s: %s", tool_name, exc)

        # Final, non-negotiable contract: the primary result is the only
        # thing returned to the live caller, regardless of what happened
        # above. The Honcho plugin never sees Fuli output.
        return primary_result

    def _mirror_write(
        self,
        tool_name: str,
        args: Dict[str, Any],
        primary_class: PrimaryResult,
        correlation_id: Optional[str] = None,
        pilot_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Mirror a write to Fuli only if the primary operation was confirmed successful.

        Returns a structured dict describing the mirror outcome so the pilot can
        record it in the ledger.
        """
        outcome: Dict[str, Any] = {
            "mirror_attempted": False,
            "memory_id": None,
            "accepted": False,
            "indexed": False,
            "pending": False,
            "failed": False,
            "error_code": None,
            "error_message": None,
            "latency_ms": 0.0,
        }
        if self._secondary is None:
            return outcome

        # Hard rule: only confirmed successful primary writes may become canonical
        # Fuli memories. Failed/initializing/rejected/etc. payloads may be recorded
        # as diagnostic events, but never as a memory.
        if not primary_class.is_confirmed_success:
            if self._store is not None:
                self._store.record_diagnostic_event(
                    namespace=self._namespace,
                    operation=tool_name,
                    event_type="mirror_skipped_primary_not_success",
                    correlation_id=correlation_id,
                    primary_status=primary_class.status.value,
                    error_message=primary_class.error_message,
                )
            outcome["error_code"] = "mirror_skipped_primary_not_success"
            outcome["error_message"] = primary_class.error_message or "primary did not succeed"
            return outcome

        start = time.monotonic()
        content, metadata = self._map_to_secondary_write(tool_name, args)
        if not content:
            return outcome

        shadow_success = False
        shadow_error: Optional[str] = None
        memory_id: Optional[str] = None
        indexed = False
        pending = False
        failed = False

        try:
            add_args = {
                "content": content,
                "source": tool_name,
                "namespace": self._namespace,
                "tags": ["shadow", "synthetic"],
            }
            if metadata:
                add_args.update(metadata)
            if self._capture_content:
                add_args["memory_type"] = metadata.get("memory_type", "shadow")
            # Inject pilot metadata so Fuli can filter/correlate later.
            pilot_payload = {"correlation_id": correlation_id}
            if pilot_meta is not None:
                pilot_payload.update(pilot_meta)
            # Fuli expects metadata as a flat dict; keep correlation_id top-level too.
            add_args["correlation_id"] = correlation_id
            add_args["metadata"] = pilot_payload

            result = self._secondary.handle_tool_call(
                "fuli_memory_add",
                {**add_args, "timeout_ms": self._write_timeout_ms},
            )
            data = json.loads(result)
            memory_id = data.get("memory_id")
            status = data.get("status", "accepted")
            indexed = status == "indexed"
            pending = status == "pending"
            failed = status == "failed" or data.get("error") is not None
            shadow_success = bool(memory_id) and not failed
            if not shadow_success and "error" in data:
                shadow_error = str(data["error"])
        except Exception as exc:
            shadow_error = str(exc)
            failed = True
            status = "failed"
        finally:
            latency_ms = (time.monotonic() - start) * 1000.0
            if self._store is not None:
                self._store.record_mirrored_write(
                    namespace=self._namespace,
                    operation=tool_name,
                    primary_success=True,
                    shadow_success=shadow_success,
                    shadow_error=shadow_error,
                    latency_ms=latency_ms,
                    content_fingerprint=self._store.fingerprint(content) if content else None,
                    correlation_id=correlation_id,
                    fuli_memory_id=memory_id,
                    metadata=pilot_meta,
                )
            outcome.update({
                "mirror_attempted": True,
                "memory_id": memory_id,
                "accepted": bool(memory_id),
                "indexed": indexed,
                "pending": pending,
                "failed": failed,
                "error_code": ("fuli_" + status) if failed else None,
                "error_message": shadow_error,
                "latency_ms": latency_ms,
            })
        return outcome

    def _compare_read(
        self,
        tool_name: str,
        args: Dict[str, Any],
        primary_result: str,
        primary_latency_ms: float,
        *,
        query_hash: str,
        decision: SamplingDecision,
    ) -> None:
        """Run a comparison between primary and secondary search results.

        Hard safety guarantees (also enforced by tests):
          - The comparison runs in a worker thread bounded by
            self._comparison_budget_ms; the primary result has already
            been returned to the caller regardless.
          - The comparison never fails the user request: all exceptions
            are caught and recorded as ``secondary_error_category``.
          - Fuli's response is never propagated into the primary result
            string. The primary string is captured at function entry; if
            it changes during comparison (it should not), the comparison
            is aborted.
          - The schema has no raw-content column; content_captured is
            always False. Any future caller that flips it would still not
            leak content, but the flag is recorded for audit.
        """
        # Hard safety: snapshot the primary result string. If anything in
        # the comparison flow ever tries to mutate it (it should not), we
        # abort the comparison.
        primary_result_snapshot = primary_result

        comparison = ComparisonRecord(
            comparison_id="",  # generated by record_comparison
            run_id=self._comparison_run_id,
            timestamp="",      # populated by record_comparison
            namespace=self._namespace,
            query_hash=query_hash,
            query_type="unclassified",
            requested_top_k=int(args.get("top_k", 5) or 5),
            primary_provider=self._primary_name,
            secondary_provider=self._secondary_name,
            primary_latency_ms=primary_latency_ms,
            secondary_latency_ms=0.0,
            primary_status="success",
            secondary_status="pending",
        )

        primary_results = self._extract_results(primary_result)
        primary_fps = result_fingerprints(primary_results, limit=comparison.requested_top_k)
        primary_error_category: Optional[str] = None
        if primary_results is None:
            primary_error_category = "primary_parse_error"

        comparison.primary_result_fingerprints = primary_fps

        secondary_results: Optional[List[Any]] = None
        secondary_error_category: Optional[str] = None
        secondary_latency_ms = 0.0

        if self._secondary is not None and primary_error_category is None:
            sec_start = time.monotonic()
            secondary_results, secondary_error_category = self._bounded_secondary_search(
                args, comparison.requested_top_k
            )
            secondary_latency_ms = (time.monotonic() - sec_start) * 1000.0
            comparison.secondary_latency_ms = secondary_latency_ms
            if secondary_error_category is None:
                comparison.secondary_status = "success"
            else:
                comparison.secondary_status = "failed"
        else:
            comparison.secondary_status = "skipped"

        secondary_fps = (
            result_fingerprints(secondary_results, limit=comparison.requested_top_k)
            if secondary_results is not None
            else []
        )
        comparison.secondary_result_fingerprints = secondary_fps

        k = comparison.requested_top_k
        comparison.overlap_at_1 = overlap_at_k(primary_fps, secondary_fps, 1)
        comparison.overlap_at_3 = overlap_at_k(primary_fps, secondary_fps, 3)
        comparison.overlap_at_5 = overlap_at_k(primary_fps, secondary_fps, k)
        comparison.reciprocal_rank_agreement = reciprocal_rank_agreement(
            primary_fps, secondary_fps
        )
        comparison.missing_from_primary = missing_from(primary_fps, secondary_fps, k)
        comparison.missing_from_secondary = missing_from(secondary_fps, primary_fps, k)
        comparison.primary_error_category = primary_error_category
        comparison.secondary_error_category = secondary_error_category
        comparison.secondary_retrieval_mode = self._detect_retrieval_mode(args)
        # content_captured is hard-locked to False. Schema has no raw-content
        # column; this flag is purely an audit trail.
        comparison.content_captured = False

        # Persist asynchronously with a hard wall-clock cap. We use a daemon
        # thread so the live request never waits on the write. If the budget
        # is exceeded the worker is left to finish in the background but the
        # caller has already moved on.
        self._persist_comparison_async(comparison)

        # Defence: confirm primary_result_snapshot wasn't mutated.
        if primary_result_snapshot != primary_result:
            logger.error(
                "Shadow primary result mutated during comparison; aborting. "
                "This is a critical invariant violation."
            )

    def _extract_results(self, primary_result: str) -> Optional[List[Any]]:
        """Parse the primary result string into a comparable list.

        Returns ``None`` on parse failure (a primary parse error is treated
        as a comparison failure, not a silent skip).
        """
        if not primary_result:
            return None
        try:
            data = json.loads(primary_result)
        except Exception:
            # Some Honcho responses are plain text; treat the whole string
            # as one result so we still get overlap metrics when possible.
            return [primary_result]
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if "results" in data and isinstance(data["results"], list):
                return data["results"]
            # Some shapes wrap a single result under various keys; try a few.
            for k in ("excerpt", "content", "answer", "text"):
                if k in data:
                    return [data[k]]
        return [data]

    def _bounded_secondary_search(
        self, args: Dict[str, Any], top_k: int
    ) -> tuple[Optional[List[Any]], Optional[str]]:
        """Call Fuli with a hard wall-clock budget; never raise."""
        assert self._secondary is not None
        search_args = {
            "query": args.get("query", ""),
            "top_k": top_k,
            "namespace": self._namespace,
            "timeout_ms": self._read_timeout_ms,
        }
        deadline = time.monotonic() + (self._comparison_budget_ms / 1000.0)
        result_container: Dict[str, Any] = {}

        def _runner() -> None:
            try:
                raw = self._secondary.handle_tool_call(
                    "fuli_memory_search", search_args
                )
                result_container["raw"] = raw
            except Exception as exc:
                result_container["error"] = str(exc)

        thread = threading.Thread(
            target=_runner,
            name=f"shadow-compare-{query_hash_for(args.get('query', ''))}",
            daemon=True,
        )
        thread.start()
        thread.join(timeout=max(0.05, deadline - time.monotonic()))
        if thread.is_alive():
            return None, f"timeout_after_{self._comparison_budget_ms}ms"
        if "error" in result_container:
            return None, f"secondary_error: {result_container['error']}"
        raw = result_container.get("raw", "")
        try:
            data = json.loads(raw)
        except Exception as exc:
            return None, f"secondary_parse_error: {exc}"
        if isinstance(data, dict):
            results = data.get("results")
            if isinstance(results, list):
                return list(results), None
            return None, "secondary_shape_error"
        if isinstance(data, list):
            return list(data), None
        return None, "secondary_shape_error"

    def _persist_comparison_async(self, comparison: ComparisonRecord) -> None:
        """Persist a comparison in a background thread.

        The thread is daemon so it cannot block process exit. If the write
        fails (DB lock, IO error), we drop the record and log loudly; the
        primary request has already been served.
        """
        store = self._comparison_store
        if store is None:
            return

        def _writer() -> None:
            try:
                store.record_comparison(comparison)
            except Exception as exc:
                logger.warning(
                    "Shadow comparison persistence failed for %s: %s",
                    comparison.query_hash,
                    exc,
                )

        threading.Thread(
            target=_writer,
            name=f"shadow-persist-{comparison.query_hash}",
            daemon=True,
        ).start()

    @staticmethod
    def _detect_retrieval_mode(args: Dict[str, Any]) -> str:
        mode = args.get("mode")
        if isinstance(mode, str) and mode:
            return mode
        return "unknown"

    @staticmethod
    def _map_to_secondary_write(tool_name: str, args: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        metadata: Dict[str, Any] = {}
        if tool_name == "honcho_conclude":
            content = args.get("conclusion", "")
            metadata["memory_type"] = "conclusion"
            metadata["peer"] = args.get("peer", "")
        elif tool_name == "honcho_profile":
            card = args.get("card")
            if not isinstance(card, list):
                return "", {}
            content = "\n".join(str(c) for c in card)
            metadata["memory_type"] = "profile"
            metadata["peer"] = args.get("peer", "")
        else:
            return "", {}
        return content, metadata

    def backup_paths(self) -> List[str]:
        paths: List[str] = []
        if self._primary is not None:
            paths.extend(self._primary.backup_paths())
        if self._secondary is not None:
            paths.extend(self._secondary.backup_paths())
        if self._store is not None:
            paths.append(str(self._store.db_path))
        return paths

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        # Persist shadow config to the provider JSON file for desktop UI parity.
        cfg_path = Path(hermes_home) / "shadow" / "config.json"
        existing: Dict[str, Any] = {}
        if cfg_path.exists():
            try:
                existing = json.loads(cfg_path.read_text())
            except Exception:
                pass
        for k, v in values.items():
            if v not in (None, ""):
                existing[k] = v
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        from utils import atomic_json_write
        atomic_json_write(cfg_path, existing, mode=0o600)

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        if self._primary is not None and hasattr(self._primary, "on_session_end"):
            try:
                self._primary.on_session_end(messages)
            except Exception as exc:
                logger.debug("Shadow primary on_session_end failed: %s", exc)
        if self._secondary is not None and hasattr(self._secondary, "on_session_end"):
            try:
                self._secondary.on_session_end(messages)
            except Exception as exc:
                logger.debug("Shadow secondary on_session_end failed: %s", exc)

    def shutdown(self) -> None:
        if self._secondary is not None:
            try:
                self._secondary.shutdown()
            except Exception as exc:
                logger.debug("Shadow secondary shutdown failed: %s", exc)
            self._secondary = None
        if self._primary is not None:
            try:
                self._primary.shutdown()
            except Exception as exc:
                logger.debug("Shadow primary shutdown failed: %s", exc)
            self._primary = None

    # -- Test hooks ------------------------------------------------------------

    def _inject_primary(self, provider: MemoryProvider) -> None:
        self._primary = provider

    def _inject_secondary(self, provider: MemoryProvider) -> None:
        self._secondary = provider

    def _report(self) -> Dict[str, Any]:
        if self._store is None:
            return {}
        return self._store.report()
