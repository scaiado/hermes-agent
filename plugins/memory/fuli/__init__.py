"""Fuli memory provider — Hermes Agent integration.

Wraps the Fuli local canonical-memory provider and exposes it to Hermes as
a standard MemoryProvider plugin. All Fuli operations are async; Hermes'
handle_tool_call() is synchronous, so we bridge through a dedicated asyncio
loop running on a background daemon thread.

Config sources (in order of precedence):
1. ``memory.fuli`` block in ``~/.hermes/config.yaml`` (legacy / setup wizard path)
2. ``~/.hermes/fuli/config.json`` (desktop UI path)
3. Defaults shown below

Defaults:
  db_path: <HERMES_HOME>/memories/fuli.db
  namespace: hermes:default
  embedding_provider: local
  embedding_model: ""   (Fuli default sentence-transformer)
  device: cpu
  lazy_init: true
  timeout_ms: 5000

The provider is initialized lazily: no SQLite/vector/embedding work happens
during plugin discovery, schema listing, or ``is_available()``. The first tool
call or explicit ``initialize(..., warm_start=True)`` triggers the build.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

ADD_SCHEMA = {
    "name": "fuli_memory_add",
    "description": (
        "Store a new memory in Fuli. Returns the memory id and status. "
        "Use source='user' for things the user said, 'agent' for things you learned, "
        "or 'observation' for background facts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "content": {"type": "string", "description": "The memory text to store."},
            "source": {"type": "string", "default": "unknown"},
            "namespace": {"type": "string", "default": "hermes:default"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "importance": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1, "default": 0.5},
            "memory_type": {"type": "string"},
            "correlation_id": {"type": "string"},
        },
        "required": ["content"],
    },
}

SEARCH_SCHEMA = {
    "name": "fuli_memory_search",
    "description": (
        "Search Fuli memories with hybrid vector + lexical ranking. "
        "Returns the top-k most relevant memories with explainable scores."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query."},
            "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
            "namespace": {"type": "string", "default": "hermes:default"},
            "source": {"type": "string"},
            "lifecycle": {"type": "string"},
        },
        "required": ["query"],
    },
}

GET_SCHEMA = {
    "name": "fuli_memory_get",
    "description": "Fetch a single Fuli memory by id.",
    "parameters": {
        "type": "object",
        "properties": {"memory_id": {"type": "string"}},
        "required": ["memory_id"],
    },
}

DELETE_SCHEMA = {
    "name": "fuli_memory_delete",
    "description": "Soft-delete a Fuli memory by id.",
    "parameters": {
        "type": "object",
        "properties": {"memory_id": {"type": "string"}},
        "required": ["memory_id"],
    },
}

REINFORCE_SCHEMA = {
    "name": "fuli_memory_reinforce",
    "description": (
        "Strengthen a memory in Fuli (bumps recency/importance signals). "
        "Use when a memory just proved useful again."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "memory_id": {"type": "string"},
            "namespace": {"type": "string"},
            "amount": {"type": "number", "default": 0.1, "minimum": 0, "maximum": 1},
        },
        "required": ["memory_id"],
    },
}

DIAGNOSTICS_SCHEMA = {
    "name": "fuli_memory_diagnostics",
    "description": "Return a snapshot of the Fuli memory store (counts, indexing status, namespaces).",
    "parameters": {"type": "object", "properties": {}, "required": []},
}

BATCH_STATUS_SCHEMA = {
    "name": "fuli_memory_batch_status",
    "description": "Return structured status for a batch of Fuli memory IDs.",
    "parameters": {
        "type": "object",
        "properties": {
            "memory_ids": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["memory_ids"],
    },
}

STORAGE_HEALTH_SCHEMA = {
    "name": "fuli_memory_storage_health",
    "description": "Return a SQLite storage health check report (journal mode, WAL size, integrity checks).",
    "parameters": {
        "type": "object",
        "properties": {
            "deep": {"type": "boolean", "default": False},
        },
        "required": [],
    },
}

ALL_SCHEMAS = [ADD_SCHEMA, SEARCH_SCHEMA, GET_SCHEMA, DELETE_SCHEMA, REINFORCE_SCHEMA, DIAGNOSTICS_SCHEMA, BATCH_STATUS_SCHEMA, STORAGE_HEALTH_SCHEMA]


# ---------------------------------------------------------------------------
# Async bridge (per-provider instance)
# ---------------------------------------------------------------------------

class _AsyncBridge:
    """Runs an asyncio event loop on a daemon thread for Fuli's async API.

    Each provider instance owns its bridge so multi-instance tests and
    shutdown are isolated. The bridge is started lazily on the first call
    and stopped cleanly by shutdown().
    """

    def __init__(self, default_timeout: float = 30.0) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._default_timeout = default_timeout

    def _start(self) -> None:
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._loop.run_forever, daemon=True, name="fuli-async")
            self._thread.start()

    def run(self, coro, timeout: Optional[float] = None) -> Any:
        """Run a coroutine on the bridge loop and return its result.

        A timeout prevents a hung call from blocking the agent forever.
        If the call times out, the underlying task is cancelled and the
        bridge is left running for subsequent calls.
        """
        if self._loop is None or not self._loop.is_running():
            self._start()
        loop = self._loop
        if loop is None:
            raise RuntimeError("Fuli async bridge loop unavailable")
        deadline = timeout if timeout is not None else self._default_timeout
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        try:
            return future.result(timeout=deadline)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise concurrent.futures.TimeoutError(f"Fuli call timed out after {deadline}s") from exc

    def stop(self, join_timeout: float = 3.0) -> None:
        """Stop the loop and join the thread with a bounded wait."""
        with self._lock:
            loop = self._loop
            thread = self._thread
            if loop is None or thread is None:
                return
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception as exc:
                logger.debug("Fuli bridge stop signal failed: %s", exc)
            self._loop = None
            self._thread = None

        if thread.is_alive():
            thread.join(timeout=join_timeout)
        if thread.is_alive():
            logger.warning("Fuli async bridge thread did not stop within %ss", join_timeout)


# ---------------------------------------------------------------------------
# MemoryProvider implementation
# ---------------------------------------------------------------------------

class FuliMemoryProvider(MemoryProvider):
    """Hermes MemoryProvider adapter for the local Fuli memory core."""

    def __init__(self) -> None:
        self._provider: Any = None
        self._db_path: Optional[str] = None
        self._hermes_home: Optional[str] = None
        self._namespace: str = "hermes:default"
        self._embedding_provider: str = "local"
        self._embedding_model: str = ""
        self._device: str = "cpu"
        self._lazy_init: bool = True
        self._timeout_ms: int = 5000
        self._bridge = _AsyncBridge(default_timeout=30.0)
        self._init_lock = threading.Lock()
        self._init_error: Optional[str] = None
        self._embedder: Any = None  # test hook; if set, used instead of _create_embedder()

    @property
    def name(self) -> str:
        return "fuli"

    def is_available(self) -> bool:
        """Fuli is available if the package can be imported. No model load."""
        try:
            import fuli  # noqa: F401
            from fuli.provider import create_provider  # noqa: F401
            return True
        except Exception as exc:
            logger.debug("Fuli not available: %s", exc)
            return False

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "db_path",
                "description": "Path to the Fuli SQLite database (default: <HERMES_HOME>/memories/fuli.db)",
                "required": False,
            },
            {
                "key": "namespace",
                "description": "Fuli namespace for Hermes memories (default: hermes:default)",
                "required": False,
            },
            {
                "key": "embedding_provider",
                "description": "Embedding backend: 'local' (sentence-transformers) or 'ollama'",
                "required": False,
                "choices": ["local", "ollama"],
            },
            {
                "key": "embedding_model",
                "description": "Model name for the chosen embedding provider (empty = Fuli default)",
                "required": False,
            },
            {
                "key": "device",
                "description": "Device for local embeddings: 'cpu', 'mps', 'cuda'",
                "required": False,
            },
            {
                "key": "ollama_host",
                "description": "Ollama server URL (only used when embedding_provider=ollama)",
                "required": False,
            },
            {
                "key": "lazy_init",
                "description": "Defer provider build until first use (recommended: true)",
                "required": False,
            },
            {
                "key": "timeout_ms",
                "description": "Per-call timeout in milliseconds (default: 5000)",
                "required": False,
            },
        ]

    def _config_path(self) -> Path:
        return Path(self._hermes_home or ".") / "fuli" / "config.json"

    def _load_config(self, hermes_home: str) -> Dict[str, Any]:
        """Load config from config.yaml and the provider JSON file."""
        from hermes_cli.config import load_config

        config: Dict[str, Any] = {}
        # 1. Provider JSON config file (desktop UI path)
        self._hermes_home = hermes_home
        cfg_path = self._config_path()
        if cfg_path.exists():
            try:
                config.update(json.loads(cfg_path.read_text()))
            except Exception as exc:
                logger.debug("Failed to read Fuli config.json: %s", exc)

        # 2. memory.fuli block in config.yaml (legacy / setup wizard path)
        try:
            hermes_config = load_config()
            mem = hermes_config.get("memory") or {}
            if isinstance(mem, dict):
                fuli_cfg = mem.get("fuli") or {}
                if isinstance(fuli_cfg, dict):
                    config.update(fuli_cfg)
        except Exception as exc:
            logger.debug("Failed to read config.yaml memory.fuli: %s", exc)

        return config

    def _apply_config(self, config: Dict[str, Any]) -> None:
        """Apply non-secret config values to instance state."""
        self._namespace = str(config.get("namespace") or self._namespace)
        self._embedding_provider = str(config.get("embedding_provider") or self._embedding_provider)
        self._embedding_model = str(config.get("embedding_model") or self._embedding_model)
        self._device = str(config.get("device") or self._device)
        self._lazy_init = bool(config.get("lazy_init", True))
        # Legacy single timeout (fallback), then operation-specific overrides.
        try:
            base_timeout = int(config.get("timeout_ms", 5000))
        except (TypeError, ValueError):
            base_timeout = 5000
        try:
            self._write_timeout_ms = int(config.get("write_timeout_ms", base_timeout))
        except (TypeError, ValueError):
            self._write_timeout_ms = base_timeout
        try:
            self._read_timeout_ms = int(config.get("read_timeout_ms", base_timeout))
        except (TypeError, ValueError):
            self._read_timeout_ms = base_timeout
        for attr in ("_write_timeout_ms", "_read_timeout_ms"):
            val = getattr(self, attr)
            if val < 100:
                setattr(self, attr, 100)

    def _timeout_for(self, tool_name: str, args: Dict[str, Any]) -> float:
        """Return the operation timeout in seconds for a given Fuli tool call.

        Order of precedence:
        1. Per-call ``timeout_ms`` override in args.
        2. Operation-specific config: write_timeout_ms for writes, read_timeout_ms for reads.
        3. Legacy timeout_ms if it was the only config present.
        """
        if "timeout_ms" in args:
            try:
                ms = int(args["timeout_ms"])
                if ms >= 100:
                    return ms / 1000.0
            except (TypeError, ValueError):
                pass
        if tool_name in {"fuli_memory_add", "fuli_memory_delete", "fuli_memory_reinforce"}:
            return self._write_timeout_ms / 1000.0
        return self._read_timeout_ms / 1000.0

    def _resolve_db_path(self, hermes_home: str, config: Dict[str, Any]) -> str:
        db_path = config.get("db_path")
        if db_path:
            return str(Path(str(db_path)).expanduser())
        memories = Path(hermes_home) / "memories"
        memories.mkdir(parents=True, exist_ok=True)
        return str(memories / "fuli.db")

    def _create_embedder(self) -> Any:
        """Build an embedding provider according to config. Overridable for tests."""
        from fuli.embeddings.local import SentenceTransformerEmbeddingProvider

        if self._embedding_provider == "ollama":
            raise NotImplementedError("Ollama embedding provider is not implemented yet")
        if self._embedding_model:
            return SentenceTransformerEmbeddingProvider(self._embedding_model, device=self._device)
        return SentenceTransformerEmbeddingProvider(device=self._device)

    def _build_provider(self) -> Any:
        """Synchronously construct the Fuli provider on the async bridge."""
        from fuli.provider import create_provider

        db_path = self._db_path
        if db_path is None:
            raise RuntimeError("Fuli db_path is not set")

        # Pre-populate the bridge loop so the first call doesn't pay loop startup cost.
        self._bridge.run(asyncio.sleep(0), timeout=5.0)

        async def _make() -> Any:
            embedder = self._embedder if self._embedder is not None else self._create_embedder()
            return await create_provider(db_path, embedder=embedder, enable_sync=False)

        return self._bridge.run(_make(), timeout=max(30.0, self._timeout_ms / 1000.0 + 5.0))

    def _ensure_provider(self) -> bool:
        """Lazily initialize the Fuli provider. Returns True if ready."""
        if self._provider is not None:
            return True
        with self._init_lock:
            if self._provider is not None:
                return True
            if self._init_error is not None:
                return False
            if self._db_path is None:
                logger.error("Fuli provider cannot initialize: missing db_path")
                self._init_error = "missing db_path"
                return False
            try:
                self._provider = self._build_provider()
            except Exception as exc:
                self._init_error = str(exc)
                logger.warning("Fuli provider initialization failed: %s", exc)
                return False
        return self._provider is not None

    def initialize(self, session_id: str, **kwargs) -> None:
        hermes_home = kwargs.get("hermes_home", "")
        if not hermes_home:
            from hermes_constants import get_hermes_home
            hermes_home = str(get_hermes_home())
        self._hermes_home = hermes_home

        config = self._load_config(hermes_home)
        self._apply_config(config)
        self._db_path = self._resolve_db_path(hermes_home, config)

        if not self._lazy_init or kwargs.get("warm_start"):
            self._ensure_provider()

    def system_prompt_block(self) -> str:
        if self._provider is None:
            return ""
        return (
            "Fuli local memory is active. "
            "Use the `fuli_memory_*` tools to store, search, retrieve, and reinforce memories."
        )

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return list(ALL_SCHEMAS)

    def batch_status(self, memory_ids: List[str]) -> Dict[str, str]:
        """Synchronous convenience wrapper used by the shadow pilot drain."""
        raw = self.handle_tool_call("fuli_memory_batch_status", {"memory_ids": memory_ids})
        try:
            data = json.loads(raw)
        except Exception:
            return {mid: "pending" for mid in memory_ids}
        statuses = data.get("statuses", {})
        out: Dict[str, str] = {}
        for mid, value in statuses.items():
            if isinstance(value, dict):
                indexing = str(value.get("indexing_status") or "").lower()
                status = str(value.get("status") or "").lower()
                if indexing == "indexed":
                    out[mid] = "indexed"
                elif indexing == "failed" or status == "failed":
                    out[mid] = "failed"
                else:
                    out[mid] = "pending"
            elif isinstance(value, str):
                out[mid] = value.lower()
            else:
                out[mid] = "pending"
        return out

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if not self._ensure_provider():
            err = self._init_error or "Fuli provider is not initialized"
            return json.dumps({"error": err})

        try:
            timeout = self._timeout_for(tool_name, args)
            if tool_name == "fuli_memory_add":
                return self._bridge.run(self._add(args), timeout=timeout)
            if tool_name == "fuli_memory_search":
                return self._bridge.run(self._search(args), timeout=timeout)
            if tool_name == "fuli_memory_get":
                return self._bridge.run(self._get(args), timeout=timeout)
            if tool_name == "fuli_memory_delete":
                return self._bridge.run(self._delete(args), timeout=timeout)
            if tool_name == "fuli_memory_reinforce":
                return self._bridge.run(self._reinforce(args), timeout=timeout)
            if tool_name == "fuli_memory_diagnostics":
                return self._bridge.run(self._diagnostics(), timeout=timeout)
            if tool_name == "fuli_memory_batch_status":
                return self._bridge.run(self._batch_status(args.get("memory_ids", [])), timeout=timeout)
            if tool_name == "fuli_memory_storage_health":
                return self._bridge.run(self._storage_health(args.get("deep", False)), timeout=timeout)
        except concurrent.futures.TimeoutError as exc:
            logger.warning("Fuli tool %s timed out: %s", tool_name, exc)
            return json.dumps({"error": f"Fuli call timed out: {exc}"})
        except Exception as exc:
            logger.warning("Fuli tool %s failed: %s", tool_name, exc)
            return json.dumps({"error": str(exc)})

        return json.dumps({"error": f"Unknown Fuli tool: {tool_name}"})

    # -- Fuli call wrappers ---------------------------------------------------

    async def _add(self, args: Dict[str, Any]) -> str:
        content = args["content"]
        source = args.get("source", "unknown")
        namespace = args.get("namespace", self._namespace)
        correlation_id = args.get("correlation_id")
        metadata: Dict[str, Any] = {}
        if "tags" in args:
            metadata["tags"] = args["tags"]
        if "importance" in args:
            metadata["importance"] = args["importance"]
        if "confidence" in args:
            metadata["confidence"] = args["confidence"]
        if "memory_type" in args:
            metadata["memory_type"] = args["memory_type"]

        # Prefer the P0 structured method when available; fall back to legacy add().
        if hasattr(self._provider, "add_with_status"):
            result = await self._provider.add_with_status(
                content, source=source, namespace=namespace, correlation_id=correlation_id, **metadata
            )
            return json.dumps({
                "memory_id": result.memory_id,
                "event_id": getattr(result, "event_id", None),
                "created": getattr(result, "created", True),
                "status": self._indexing_status_name(result.indexing_status),
                "indexing_error": getattr(result, "indexing_error", None),
                "correlation_id": getattr(result, "correlation_id", None),
            })

        memory_id = await self._provider.add(content, source=source, namespace=namespace, correlation_id=correlation_id, **metadata)
        return json.dumps({"memory_id": memory_id})

    @staticmethod
    def _indexing_status_name(status: Any) -> str:
        return status.value if hasattr(status, "value") else str(status)

    async def _search(self, args: Dict[str, Any]) -> str:
        from fuli.models import RetrievalMode, SearchFilters

        query = args["query"]
        top_k = args.get("top_k", 5)
        namespace = args.get("namespace")
        filters = SearchFilters()
        if "source" in args:
            filters.source = args["source"]
        if "lifecycle" in args:
            from fuli.models import MemoryLifecycle
            filters.lifecycle = MemoryLifecycle(args["lifecycle"])

        mode = RetrievalMode.HYBRID
        if args.get("mode") in {"vector", "lexical", "text"}:
            mode = RetrievalMode(args["mode"])

        results = await self._provider.search(
            query, top_k=top_k, namespace=namespace, filters=filters, mode=mode
        )
        return json.dumps({
            "results": [
                {
                    "id": r.memory.id,
                    "content": r.memory.content,
                    "source": r.memory.source,
                    "namespace": r.memory.namespace,
                    "lifecycle": r.memory.lifecycle.value,
                    "score": r.scores.final_score,
                    "scores": r.scores.model_dump(),
                }
                for r in results
            ]
        })

    async def _get(self, args: Dict[str, Any]) -> str:
        record = await self._provider.get(args["memory_id"])
        if record is None:
            return json.dumps({"error": "Memory not found"})
        return json.dumps({
            "id": record.id,
            "content": record.content,
            "source": record.source,
            "namespace": record.namespace,
            "lifecycle": record.lifecycle.value,
            "metadata": record.metadata,
            "created_at": record.created_at,
        })

    async def _delete(self, args: Dict[str, Any]) -> str:
        deleted = await self._provider.delete(args["memory_id"])
        return json.dumps({"deleted": deleted})

    async def _reinforce(self, args: Dict[str, Any]) -> str:
        memory_id = args["memory_id"]
        namespace = args.get("namespace")
        amount = args.get("amount", 0.1)
        record = await self._provider.reinforce(memory_id, namespace=namespace, amount=amount)
        if record is None:
            return json.dumps({"error": "Memory not found"})
        return json.dumps({"id": record.id, "reinforcement_count": record.reinforcement_count})

    async def _diagnostics(self) -> str:
        from fuli.diagnostics import gather_diagnostics

        report = await gather_diagnostics(self._provider, Path(self._db_path or ":memory:"))
        return json.dumps({
            "db_path": report.db_path,
            "total_memories": report.total_memories,
            "active_memories": report.active_memories,
            "deleted_memories": report.deleted_memories,
            "indexed_count": report.indexed_count,
            "pending_count": report.pending_count,
            "failed_count": report.failed_count,
            "namespaces": report.namespaces,
            "lifecycle_distribution": report.lifecycle_distribution,
        })

    async def _batch_status(self, memory_ids: List[str]) -> str:
        if not hasattr(self._provider, "batch_status"):
            return json.dumps({"error": "batch_status not supported by this provider"})
        result = await self._provider.batch_status(memory_ids=memory_ids)
        out: Dict[str, Any] = {}
        for mid, value in result.items():
            if value is None:
                out[mid] = None
            elif hasattr(value, "memory_id"):
                out[mid] = {
                    "memory_id": value.memory_id,
                    "event_id": getattr(value, "event_id", None),
                    "created": getattr(value, "created", True),
                    "status": self._indexing_status_name(getattr(value, "status", None)),
                    "indexing_status": self._indexing_status_name(getattr(value, "indexing_status", None)),
                    "indexing_error": getattr(value, "indexing_error", None),
                    "correlation_id": getattr(value, "correlation_id", None),
                    "duplicate_of": getattr(value, "duplicate_of", None),
                }
            else:
                out[mid] = str(value)
        return json.dumps({"statuses": out})

    async def _storage_health(self, deep: bool = False) -> str:
        report = await self._provider.run_storage_health_check(include_integrity=deep)
        return report.model_dump_json()

    # -- Lifecycle hooks ------------------------------------------------------

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Best-effort: sync the last user/assistant exchange as a memory."""
        if not self._ensure_provider() or not messages:
            return
        try:
            last_user = None
            last_assistant = None
            for m in reversed(messages):
                if m.get("role") == "user" and last_user is None:
                    last_user = m.get("content", "")
                if m.get("role") == "assistant" and last_assistant is None:
                    last_assistant = m.get("content", "")
                if last_user and last_assistant:
                    break
            if last_user:
                self._bridge.run(
                    self._add_with_fallback(last_user, source="user", namespace=self._namespace, tags=["session_end"]),
                    timeout=self._timeout_ms / 1000.0,
                )
            if last_assistant:
                self._bridge.run(
                    self._add_with_fallback(last_assistant, source="agent", namespace=self._namespace, tags=["session_end"]),
                    timeout=self._timeout_ms / 1000.0,
                )
        except Exception as exc:
            logger.debug("Fuli on_session_end failed: %s", exc)

    async def _add_with_fallback(self, content: str, source: str, namespace: str, **metadata: Any) -> Any:
        """Use add_with_status if available, otherwise fall back to legacy add()."""
        if hasattr(self._provider, "add_with_status"):
            return await self._provider.add_with_status(content, source=source, namespace=namespace, **metadata)
        return await self._provider.add(content, source=source, namespace=namespace, **metadata)

    def backup_paths(self) -> List[str]:
        """Declare the Fuli database so `hermes backup` captures it."""
        if self._db_path:
            return [self._db_path]
        return []

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret Fuli config to the provider JSON file."""
        cfg_path = Path(hermes_home) / "fuli" / "config.json"
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

    def shutdown(self) -> None:
        """Clean shutdown: stop the async bridge with a bounded wait."""
        try:
            provider = self._provider
            if provider is not None and hasattr(provider, "shutdown"):
                self._bridge.run(provider.shutdown(), timeout=5.0)
        except Exception as exc:
            logger.debug("Fuli provider shutdown failed: %s", exc)
        finally:
            self._provider = None
            self._bridge.stop(join_timeout=3.0)

    # -- Test hooks ------------------------------------------------------------

    def _inject_embedder(self, embedder: Any) -> None:
        """Test hook: replace the embedder without building the real model."""
        self._embedder = embedder
