"""Isolated shadow memory smoke test.

Runs a temporary Hermes home with memory.provider="shadow" and exercises the
end-to-end write mirror and read comparison path. Uses a fake primary (Honcho)
and a fake embedder for Fuli so no real services or model downloads are needed.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any

from agent.memory_provider import MemoryProvider


class FakeEmbeddingProvider:
    dimension = 8

    async def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def _vector(self, text: str) -> list[float]:
        import hashlib
        digest = hashlib.sha256(text.encode("utf-8")).digest()[:8]
        vec = [(b + 1) / 256.0 for b in digest]
        norm = sum(v * v for v in vec) ** 0.5
        if norm == 0:
            return vec
        return [v / norm for v in vec]


class FakeHonchoProvider(MemoryProvider):
    """Mimics the live Honcho surface for shadow tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._schemas = [
            {
                "name": "honcho_search",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            },
            {
                "name": "honcho_conclude",
                "parameters": {"type": "object", "properties": {"conclusion": {"type": "string"}, "peer": {"type": "string"}}, "required": []},
            },
            {
                "name": "honcho_profile",
                "parameters": {"type": "object", "properties": {"peer": {"type": "string"}, "card": {"type": "array"}}, "required": []},
            },
        ]

    @property
    def name(self) -> str:
        return "honcho"

    def is_available(self) -> bool:
        return True

    def get_config_schema(self) -> list[dict[str, Any]]:
        return []

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return self._schemas

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        self.calls.append((tool_name, args))
        if tool_name == "honcho_search":
            return json.dumps(["Primary result about tea"])
        return json.dumps({"ok": True, "tool": tool_name})

    def backup_paths(self) -> list[str]:
        return []

    def shutdown(self) -> None:
        pass


def run() -> dict[str, Any]:
    tmpdir = tempfile.mkdtemp(prefix="shadow_smoke_")
    try:
        hermes_home = Path(tmpdir)
        config = hermes_home / "config.yaml"
        config.write_text(
            "\n".join(
                [
                    "memory:",
                    "  provider: shadow",
                    "  shadow:",
                    "    enabled: true",
                    "    primary_provider: honcho",
                    "    secondary_provider: fuli",
                    "    mirror_writes: true",
                    "    compare_reads: true",
                    "    sample_rate: 1.0",
                    "    timeout_ms: 250",
                    "    capture_content: false",
                    "    namespace: hermes:smoke",
                ]
            )
            + "\n"
        )
        os.environ["HERMES_HOME"] = str(hermes_home)

        from plugins.memory import load_memory_provider
        from plugins.memory.fuli import FuliMemoryProvider
        from plugins.memory.shadow import ShadowMemoryProvider

        # Build the real providers with test hooks.
        primary = FakeHonchoProvider()
        secondary = FuliMemoryProvider()
        secondary._inject_embedder(FakeEmbeddingProvider())
        secondary.initialize("smoke-secondary", hermes_home=str(hermes_home))

        shadow = ShadowMemoryProvider()
        shadow._inject_primary(primary)
        shadow._inject_secondary(secondary)
        shadow.initialize("smoke-session", hermes_home=str(hermes_home))

        unique = f"Shadow smoke fact thread {threading.current_thread().ident}"

        # 1. Primary write result returned unchanged
        write = shadow.handle_tool_call("honcho_conclude", {"conclusion": unique, "peer": "user"})
        assert json.loads(write)["ok"] is True, write

        # 2. Write appears in Fuli
        fuli_search = json.loads(secondary.handle_tool_call("fuli_memory_search", {"query": unique, "top_k": 5}))
        assert any(unique in r["content"] for r in fuli_search["results"]), fuli_search

        # 3. Primary search result returned unchanged
        search = shadow.handle_tool_call("honcho_search", {"query": "tea"})
        assert json.loads(search) == ["Primary result about tea"], search

        # 5. Evidence row recorded
        report = shadow._report()
        assert report["writes"]["attempted"] == 1
        assert report["reads"]["sample_count"] == 1

        # 6. Query content not recorded (only fingerprints)
        # The evidence store only stores hashes, not raw query text.
        from plugins.memory.shadow.shadow_store import ShadowEvidenceStore

        store = ShadowEvidenceStore(hermes_home / "memories" / "shadow.db")
        rows = list(store._connect().execute("SELECT query_hash FROM observations"))
        assert len(rows) == 1
        assert rows[0]["query_hash"] != "tea"

        # 7. Overlap metrics populated (None if no overlap, but the column is set)
        assert "overlap_at_1" in report["reads"]

        # 8. Fuli failure simulation remains fail-open
        failing_secondary = FuliMemoryProvider()
        failing_secondary._inject_embedder(FakeEmbeddingProvider())
        failing_secondary.initialize("smoke-failing", hermes_home=str(hermes_home))
        # Force a failing secondary by breaking its db path after init.
        failing_secondary._db_path = "/dev/null/broken.db"
        shadow._inject_secondary(failing_secondary)
        search2 = shadow.handle_tool_call("honcho_search", {"query": "tea"})
        assert json.loads(search2) == ["Primary result about tea"], search2

        # 9. Shutdown cleans resources
        shadow.shutdown()
        assert shadow._primary is None
        assert shadow._secondary is None

        return {"status": "ok", "tmpdir": tmpdir}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
