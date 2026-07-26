"""Tests for the Fuli MemoryProvider plugin.

These tests use a deterministic fake embedding provider so the Hermes test suite
never downloads a sentence-transformer model or accesses the network.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import pytest

from plugins.memory.fuli import FuliMemoryProvider


class FakeEmbeddingProvider:
    """Deterministic fake embedder for tests.

    Uses a fixed 8-dimensional vector space derived from the content string so
    tests are hermetic and repeatable across machines.
    """

    dimension = 8

    async def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        import hashlib
        digest = hashlib.sha256(text.encode("utf-8")).digest()[:8]
        vec = [(b + 1) / 256.0 for b in digest]
        norm = sum(v * v for v in vec) ** 0.5
        if norm == 0:
            return vec
        return [v / norm for v in vec]


class FailingEmbeddingProvider(FakeEmbeddingProvider):
    """Fake embedder that fails once with Fuli's EmbeddingError, then succeeds."""

    def __init__(self) -> None:
        self.attempts = 0

    async def encode(self, texts: list[str]) -> list[list[float]]:
        from fuli.errors import EmbeddingError
        self.attempts += 1
        if self.attempts == 1:
            raise EmbeddingError("first attempt fails")
        return [self._vector(text) for text in texts]


@pytest.fixture
def fake_embedder() -> FakeEmbeddingProvider:
    return FakeEmbeddingProvider()


@pytest.fixture
def hermes_home(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def provider(hermes_home: Path, fake_embedder: FakeEmbeddingProvider):
    p = FuliMemoryProvider()
    p._inject_embedder(fake_embedder)
    p.initialize("test-session", hermes_home=str(hermes_home))
    yield p
    p.shutdown()


@pytest.fixture
def isolated_provider():
    p = FuliMemoryProvider()
    yield p
    p.shutdown()


def test_provider_name():
    p = FuliMemoryProvider()
    assert p.name == "fuli"
    p.shutdown()


def test_is_available_does_not_download_model():
    p = FuliMemoryProvider()
    assert p.is_available() is True
    assert p._provider is None
    p.shutdown()


def test_lazy_initialization(hermes_home: Path, fake_embedder: FakeEmbeddingProvider):
    p = FuliMemoryProvider()
    p._inject_embedder(fake_embedder)
    p.initialize("test-session", hermes_home=str(hermes_home))
    assert p._provider is None
    assert (hermes_home / "memories" / "fuli.db").exists() is False

    result = p.handle_tool_call("fuli_memory_diagnostics", args={}, tool_call_id="tc-1")
    data = json.loads(result)
    assert "error" not in data
    assert Path(data["db_path"]).exists()
    p.shutdown()


def test_add_and_search(provider: FuliMemoryProvider):
    unique = f"Fuli test fact from thread {threading.current_thread().ident}"
    add_result = provider.handle_tool_call(
        "fuli_memory_add",
        {"content": unique, "source": "test", "tags": ["test"]},
        tool_call_id="tc-1",
    )
    add_data = json.loads(add_result)
    assert "memory_id" in add_data

    search_result = provider.handle_tool_call(
        "fuli_memory_search",
        {"query": unique, "top_k": 5},
        tool_call_id="tc-2",
    )
    search_data = json.loads(search_result)
    assert len(search_data["results"]) >= 1
    assert any(unique in r["content"] for r in search_data["results"])


def test_get_and_delete(provider: FuliMemoryProvider):
    content = "temporary fact to delete"
    add_data = json.loads(
        provider.handle_tool_call("fuli_memory_add", {"content": content}, tool_call_id="tc-1")
    )
    memory_id = add_data["memory_id"]

    get_data = json.loads(provider.handle_tool_call("fuli_memory_get", {"memory_id": memory_id}, tool_call_id="tc-2"))
    assert get_data["content"] == content

    del_data = json.loads(provider.handle_tool_call("fuli_memory_delete", {"memory_id": memory_id}, tool_call_id="tc-3"))
    assert del_data["deleted"] is True

    search_data = json.loads(
        provider.handle_tool_call("fuli_memory_search", {"query": content, "top_k": 5}, tool_call_id="tc-4")
    )
    assert not any(r["content"] == content for r in search_data["results"])


def test_reinforce(provider: FuliMemoryProvider):
    content = "fact to reinforce"
    add_data = json.loads(provider.handle_tool_call("fuli_memory_add", {"content": content}, tool_call_id="tc-1"))
    memory_id = add_data["memory_id"]

    result = provider.handle_tool_call(
        "fuli_memory_reinforce", {"memory_id": memory_id, "amount": 0.2}, tool_call_id="tc-2"
    )
    data = json.loads(result)
    assert data["id"] == memory_id


def test_diagnostics(provider: FuliMemoryProvider):
    result = provider.handle_tool_call("fuli_memory_diagnostics", {}, tool_call_id="tc-1")
    data = json.loads(result)
    assert "total_memories" in data
    assert "active_memories" in data
    assert "namespaces" in data


def test_duplicate_add_returns_same_id(provider: FuliMemoryProvider):
    content = "duplicate content"
    args = {"content": content, "source": "test"}
    first = json.loads(provider.handle_tool_call("fuli_memory_add", args, tool_call_id="tc-1"))
    second = json.loads(provider.handle_tool_call("fuli_memory_add", args, tool_call_id="tc-2"))
    assert first["memory_id"] == second["memory_id"]


def test_namespace_isolation(provider: FuliMemoryProvider):
    args_a = {"content": "namespace alpha fact", "namespace": "hermes:alpha"}
    args_b = {"content": "namespace beta fact", "namespace": "hermes:beta"}
    id_a = json.loads(provider.handle_tool_call("fuli_memory_add", args_a, tool_call_id="tc-1"))["memory_id"]
    id_b = json.loads(provider.handle_tool_call("fuli_memory_add", args_b, tool_call_id="tc-2"))["memory_id"]
    assert id_a != id_b

    search_alpha = json.loads(
        provider.handle_tool_call(
            "fuli_memory_search", {"query": "namespace", "namespace": "hermes:alpha", "top_k": 5}, tool_call_id="tc-3"
        )
    )
    assert all(r["namespace"] == "hermes:alpha" for r in search_alpha["results"])


def test_repeated_shutdown_is_safe(hermes_home: Path, fake_embedder: FakeEmbeddingProvider):
    p = FuliMemoryProvider()
    p._inject_embedder(fake_embedder)
    p.initialize("test-session", hermes_home=str(hermes_home))
    p.handle_tool_call("fuli_memory_diagnostics", {}, tool_call_id="tc-1")
    p.shutdown()
    p.shutdown()
    assert p._provider is None


def test_invalid_tool_returns_error(provider: FuliMemoryProvider):
    result = provider.handle_tool_call("fuli_memory_unknown", {}, tool_call_id="tc-1")
    data = json.loads(result)
    assert "error" in data


def test_unwritable_db_directory():
    p = FuliMemoryProvider()
    if os.name == "nt":
        bad_path = "C:\\Windows\\System32\\config\\fuli_test.db"
    else:
        bad_path = "/dev/null/fuli.db"
    p._db_path = bad_path
    result = p.handle_tool_call("fuli_memory_diagnostics", {}, tool_call_id="tc-1")
    data = json.loads(result)
    assert "error" in data
    p.shutdown()


def test_embedder_failure_survives(hermes_home: Path):
    p = FuliMemoryProvider()
    p._inject_embedder(FailingEmbeddingProvider())
    p.initialize("test-session", hermes_home=str(hermes_home))
    result = p.handle_tool_call("fuli_memory_add", {"content": "fact"}, tool_call_id="tc-1")
    data = json.loads(result)
    assert "memory_id" in data
    get_data = json.loads(p.handle_tool_call("fuli_memory_get", {"memory_id": data["memory_id"]}, tool_call_id="tc-2"))
    assert get_data["content"] == "fact"
    p.shutdown()


def test_setup_registry_includes_fuli():
    from hermes_cli.memory_providers import MEMORY_PROVIDERS
    assert "fuli" in MEMORY_PROVIDERS
    fields = {f.key for f in MEMORY_PROVIDERS["fuli"].fields}
    assert {"db_path", "namespace", "embedding_provider", "timeout_ms"}.issubset(fields)


def test_save_config_creates_provider_config(hermes_home: Path):
    p = FuliMemoryProvider()
    p.save_config({"namespace": "hermes:cli", "timeout_ms": 3000}, str(hermes_home))
    cfg_path = hermes_home / "fuli" / "config.json"
    assert cfg_path.exists()
    data = json.loads(cfg_path.read_text())
    assert data["namespace"] == "hermes:cli"
    assert data["timeout_ms"] == 3000


def test_tool_schemas_have_required_fields():
    p = FuliMemoryProvider()
    schemas = p.get_tool_schemas()
    assert len(schemas) == 8
    for s in schemas:
        assert "name" in s
        assert "parameters" in s
        assert "type" in s["parameters"]
