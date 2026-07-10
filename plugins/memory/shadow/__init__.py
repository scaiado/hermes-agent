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
  timeout_ms: 250
  capture_content: false
  namespace: hermes:default

When ``enabled`` is false, the shadow provider behaves exactly like the
primary provider alone (no Fuli tools, no extra work).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
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
        self._timeout_ms: int = 250
        self._capture_content: bool = False
        self._namespace: str = "hermes:default"
        self._hermes_home: Optional[str] = None
        self._store: Optional[ShadowEvidenceStore] = None
        self._init_kwargs: Dict[str, Any] = {}

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
            {"key": "timeout_ms", "description": "Secondary provider timeout in milliseconds.", "required": False},
            {"key": "capture_content", "description": "Store full content in evidence (default false).", "required": False},
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
            self._timeout_ms = int(shadow.get("timeout_ms", 250))
        except (TypeError, ValueError):
            self._timeout_ms = 250
        self._capture_content = bool(shadow.get("capture_content", False))
        self._namespace = str(shadow.get("namespace", self._namespace))

    def _load_provider(self, name: str) -> Optional[MemoryProvider]:
        if name == "shadow":
            raise ValueError("Shadow provider cannot recursively load itself")
        return load_memory_provider(name)

    def _configure_secondary(self) -> None:
        """Ensure the secondary provider has a strict timeout and namespace.

        Writes a provider JSON config for Fuli so its own handle_tool_call
        respects the shadow timeout. This is done before initializing Fuli.
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
        existing.setdefault("timeout_ms", self._timeout_ms)
        existing.setdefault("lazy_init", True)
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(cfg_path, existing, mode=0o600)

    def _should_sample(self) -> bool:
        if self._sample_rate <= 0.0:
            return False
        if self._sample_rate >= 1.0:
            return True
        # Deterministic sampling based on a hash of the current time (stable
        # enough for tests and avoids a global counter).
        import hashlib
        bucket = int(hashlib.sha256(str(time.time()).encode()).hexdigest()[:8], 16) % 10000
        return bucket < int(self._sample_rate * 10000)

    def initialize(self, session_id: str, **kwargs) -> None:
        self._init_kwargs = dict(kwargs)
        self._hermes_home = kwargs.get("hermes_home", "")
        if not self._hermes_home:
            from hermes_constants import get_hermes_home
            self._hermes_home = str(get_hermes_home())

        self._apply_config(self._load_config())
        self._store = ShadowEvidenceStore(Path(self._hermes_home) / "memories" / "shadow.db")

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

        try:
            if tool_name in WRITE_TOOLS and self._mirror_writes:
                self._mirror_write(tool_name, args, primary_result)
            elif tool_name in READ_TOOLS and self._compare_reads and self._should_sample():
                self._compare_read(tool_name, args, primary_result, primary_latency_ms)
        except Exception as exc:
            logger.debug("Shadow secondary work failed for %s: %s", tool_name, exc)

        return primary_result

    def _mirror_write(self, tool_name: str, args: Dict[str, Any], primary_result: str) -> None:
        if self._secondary is None:
            return
        start = time.monotonic()
        content, metadata = self._map_to_secondary_write(tool_name, args)
        if not content:
            return
        shadow_success = False
        shadow_error: Optional[str] = None
        try:
            add_args = {
                "content": content,
                "source": tool_name,
                "namespace": self._namespace,
            }
            if metadata:
                add_args.update(metadata)
            if self._capture_content:
                add_args["memory_type"] = metadata.get("memory_type", "shadow")
            result = self._secondary.handle_tool_call("fuli_memory_add", add_args)
            data = json.loads(result)
            shadow_success = "memory_id" in data
            if not shadow_success and "error" in data:
                shadow_error = str(data["error"])
        except Exception as exc:
            shadow_error = str(exc)
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
                )

    def _compare_read(
        self, tool_name: str, args: Dict[str, Any], primary_result: str, primary_latency_ms: float
    ) -> None:
        if self._secondary is None or self._store is None:
            return
        if tool_name not in {"honcho_search"}:
            # Only search has a clean comparable query/content shape.
            return

        query = args.get("query", "")
        if not query:
            return

        start = time.monotonic()
        primary_fps: List[str] = []
        shadow_fps: List[str] = []
        error_category: Optional[str] = None
        try:
            primary_data = json.loads(primary_result)
            # Honcho search returns raw excerpts; the shape is a list of strings.
            excerpts = primary_data if isinstance(primary_data, list) else primary_data.get("results", [])
            primary_fps = [self._store.fingerprint(str(ex)) for ex in excerpts]
        except Exception as exc:
            error_category = f"primary_parse_error:{exc}"

        try:
            search_args = {"query": query, "top_k": 5, "namespace": self._namespace}
            shadow_result = self._secondary.handle_tool_call("fuli_memory_search", search_args)
            shadow_data = json.loads(shadow_result)
            results = shadow_data.get("results", [])
            shadow_fps = [self._store.fingerprint(r.get("content", "")) for r in results]
        except Exception as exc:
            error_category = error_category or f"shadow_error:{exc}"

        shadow_latency_ms = (time.monotonic() - start) * 1000.0

        overlaps = self._overlap_scores(primary_fps, shadow_fps)
        missing = [fp for fp in primary_fps[:5] if fp not in shadow_fps[:5]]
        self._store.record_observation(
            query_hash=self._store.query_hash(query),
            namespace=self._namespace,
            primary_latency_ms=primary_latency_ms,
            shadow_latency_ms=shadow_latency_ms,
            primary_fingerprints=primary_fps[:5],
            shadow_fingerprints=shadow_fps[:5],
            overlap_at_1=overlaps[0],
            overlap_at_3=overlaps[1],
            overlap_at_5=overlaps[2],
            missing_primary_fingerprints=missing,
            error_category=error_category,
            shadow_mode="compare",
        )

    @staticmethod
    def _overlap_scores(primary: List[str], shadow: List[str]) -> List[Optional[float]]:
        def _overlap(k: int) -> Optional[float]:
            p_set = set(primary[:k])
            s_set = set(shadow[:k])
            if not p_set:
                return None
            return len(p_set & s_set) / len(p_set)
        return [_overlap(k) for k in (1, 3, 5)]

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
