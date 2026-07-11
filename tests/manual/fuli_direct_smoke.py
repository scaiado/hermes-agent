"""Isolated direct-Fuli smoke test.

Runs a temporary Hermes home with memory.provider="fuli" and exercises the
full tool surface. Uses a fake embedder so no model is downloaded.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Any


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


def run() -> dict[str, Any]:
    tmpdir = tempfile.mkdtemp(prefix="fuli_direct_smoke_")
    try:
        hermes_home = Path(tmpdir)
        config = hermes_home / "config.yaml"
        config.write_text("memory:\n  provider: fuli\n  fuli:\n    db_path: ''\n    namespace: hermes:smoke\n    lazy_init: true\n")

        os.environ["HERMES_HOME"] = str(hermes_home)

        from plugins.memory import load_memory_provider

        provider = load_memory_provider("fuli")
        assert provider is not None, "Fuli provider failed to load"
        provider._inject_embedder(FakeEmbeddingProvider())
        provider.initialize("smoke-session", hermes_home=str(hermes_home))

        unique = f"Hermes smoke fact on thread {threading.current_thread().ident}"

        add = json.loads(provider.handle_tool_call("fuli_memory_add", {"content": unique}, tool_call_id="tc-1"))
        assert "memory_id" in add, add

        search = json.loads(provider.handle_tool_call("fuli_memory_search", {"query": unique, "top_k": 5}, tool_call_id="tc-2"))
        assert any(unique in r["content"] for r in search["results"]), search

        get = json.loads(provider.handle_tool_call("fuli_memory_get", {"memory_id": add["memory_id"]}, tool_call_id="tc-3"))
        assert get["content"] == unique, get

        reinforce = json.loads(provider.handle_tool_call("fuli_memory_reinforce", {"memory_id": add["memory_id"], "amount": 0.2}, tool_call_id="tc-4"))
        assert reinforce["id"] == add["memory_id"], reinforce

        diag = json.loads(provider.handle_tool_call("fuli_memory_diagnostics", {}, tool_call_id="tc-5"))
        assert diag["active_memories"] >= 1, diag

        deleted = json.loads(provider.handle_tool_call("fuli_memory_delete", {"memory_id": add["memory_id"]}, tool_call_id="tc-6"))
        assert deleted["deleted"] is True, deleted

        search_after = json.loads(provider.handle_tool_call("fuli_memory_search", {"query": unique, "top_k": 5}, tool_call_id="tc-7"))
        assert not any(unique in r["content"] for r in search_after["results"]), search_after

        provider.shutdown()
        return {"status": "ok", "tmpdir": tmpdir, "memory_id": add["memory_id"]}
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
