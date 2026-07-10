"""Real-embedding Fuli smoke test.

Uses the local sentence-transformers embedder with a temporary Hermes home
and temporary SQLite DB. Reports whether the model was already cached and
measures first-use initialization separately from normal operation latency.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from plugins.memory.fuli import FuliMemoryProvider


def run() -> dict:
    tmpdir = tempfile.mkdtemp(prefix="fuli_real_smoke_")
    home = Path(tmpdir)
    os.environ["HERMES_HOME"] = str(home)

    model_cached = (
        Path.home() / ".cache" / "huggingface" / "hub" / "models--sentence-transformers--all-MiniLM-L6-v2"
    ).exists()
    print(f"Model already cached: {model_cached}")

    p = FuliMemoryProvider()
    p.initialize("real-smoke", hermes_home=str(home))

    # First call triggers embedder/model load; measure it separately.
    unique = f"Real embedding smoke fact {time.time_ns()}"
    start = time.monotonic()
    add_result = json.loads(p.handle_tool_call("fuli_memory_add", {"content": unique, "namespace": "hermes:real-smoke"}))
    memory_id = add_result.get("memory_id")
    first_call_latency_ms = (time.monotonic() - start) * 1000.0

    # Normal operation after initialization.
    start = time.monotonic()
    search_result = p.handle_tool_call("fuli_memory_search", {"query": unique, "top_k": 5, "namespace": "hermes:real-smoke"})
    search_latency_ms = (time.monotonic() - start) * 1000.0

    parsed = json.loads(search_result)
    found = any(unique in r["content"] for r in parsed.get("results", []))

    # Restart provider (same DB).
    p.shutdown()
    p2 = FuliMemoryProvider()
    p2.initialize("real-smoke", hermes_home=str(home))

    restart_search = p2.handle_tool_call("fuli_memory_search", {"query": unique, "top_k": 5, "namespace": "hermes:real-smoke"})
    restart_found = any(unique in r["content"] for r in json.loads(restart_search).get("results", []))

    # Get by id and delete.
    get_result = json.loads(p2.handle_tool_call("fuli_memory_get", {"memory_id": memory_id, "namespace": "hermes:real-smoke"}))
    got_id = (get_result.get("id") if memory_id else None)
    if memory_id:
        p2.handle_tool_call("fuli_memory_delete", {"memory_id": memory_id, "namespace": "hermes:real-smoke"})
    deleted_search = p2.handle_tool_call("fuli_memory_search", {"query": unique, "top_k": 5, "namespace": "hermes:real-smoke"})
    deleted_found = any(unique in r["content"] for r in json.loads(deleted_search).get("results", []))

    p2.shutdown()
    shutil.rmtree(tmpdir, ignore_errors=True)

    return {
        "status": "ok" if (found and restart_found and not deleted_found and got_id == memory_id) else "fail",
        "model_cached": model_cached,
        "first_call_latency_ms": round(first_call_latency_ms, 2),
        "search_latency_ms": round(search_latency_ms, 2),
        "first_call_seconds": round(first_call_latency_ms / 1000, 2),
        "memory_id": memory_id,
        "tmpdir": tmpdir,
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
