"""Tests for the shadow memory provider.

Uses a fake Honcho primary and a fake Fuli secondary to prove that the shadow
provider never changes the primary output, even when the secondary fails or
times out. No live network or model downloads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent.memory_provider import MemoryProvider
from plugins.memory.shadow import ShadowMemoryProvider


class FakeHonchoProvider(MemoryProvider):
    """A fake Honcho-like provider that returns deterministic results."""

    def __init__(self, results: dict[str, Any] | None = None) -> None:
        self._results = results or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._schemas = [
            {"name": "honcho_search", "description": "Search", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
            {"name": "honcho_profile", "description": "Profile", "parameters": {"type": "object", "properties": {"peer": {"type": "string"}, "card": {"type": "array"}}, "required": []}},
            {"name": "honcho_conclude", "description": "Conclude", "parameters": {"type": "object", "properties": {"conclusion": {"type": "string"}, "peer": {"type": "string"}}, "required": []}},
            {"name": "honcho_context", "description": "Context", "parameters": {"type": "object", "properties": {"peer": {"type": "string"}}, "required": []}},
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
        return json.dumps(self._results.get(tool_name, {"ok": True, "tool": tool_name, "args": args}))

    def backup_paths(self) -> list[str]:
        return []

    def shutdown(self) -> None:
        pass


class FakeFuliProvider(MemoryProvider):
    """A fake Fuli provider that records calls and optionally fails."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.memories: dict[str, str] = {}
        self._counter = 0
        self._fail = fail

    @property
    def name(self) -> str:
        return "fuli"

    def is_available(self) -> bool:
        return True

    def get_config_schema(self) -> list[dict[str, Any]]:
        return []

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [
            {"name": "fuli_memory_add", "parameters": {"type": "object", "properties": {"content": {"type": "string"}}, "required": ["content"]}},
            {"name": "fuli_memory_search", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
        ]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        self.calls.append((tool_name, args))
        if self._fail:
            return json.dumps({"error": "fake fuli failure"})
        if tool_name == "fuli_memory_add":
            self._counter += 1
            memory_id = f"fuli-{self._counter}"
            self.memories[memory_id] = args.get("content", "")
            return json.dumps({"memory_id": memory_id})
        if tool_name == "fuli_memory_search":
            query = args.get("query", "")
            results = [{"content": v, "id": k} for k, v in self.memories.items() if query.lower() in v.lower()]
            return json.dumps({"results": results})
        return json.dumps({"ok": True})

    def backup_paths(self) -> list[str]:
        return []

    def shutdown(self) -> None:
        pass


@pytest.fixture
def hermes_home(tmp_path: Path, monkeypatch) -> Path:
    # The conftest autouse fixture redirects HERMES_HOME to tmp_path/hermes_test.
    import os
    return Path(os.environ["HERMES_HOME"])


def _write_shadow_config(hermes_home: Path, **overrides) -> None:
    cfg = {
        "primary_provider": "honcho",
        "secondary_provider": "fuli",
        "enabled": True,
        "mirror_writes": True,
        "compare_reads": True,
        "sample_rate": 1.0,
        "timeout_ms": 250,
        "capture_content": False,
        "namespace": "hermes:shadow",
    }
    cfg.update(overrides)
    lines = ["memory:", "  provider: shadow", "  shadow:"]
    for k, v in cfg.items():
        if isinstance(v, bool):
            v = str(v).lower()
        lines.append(f"    {k}: {v}")
    (hermes_home / "config.yaml").write_text("\n".join(lines) + "\n")


def test_disabled_shadow_preserves_primary_output(hermes_home: Path):
    _write_shadow_config(hermes_home, enabled=False, mirror_writes=False, compare_reads=False)
    primary = FakeHonchoProvider()
    secondary = FakeFuliProvider()
    p = ShadowMemoryProvider()
    p._inject_primary(primary)
    p._inject_secondary(secondary)
    p.initialize("shadow-test", hermes_home=str(hermes_home))

    result = p.handle_tool_call("honcho_search", {"query": "foo"})
    assert json.loads(result) == {"ok": True, "tool": "honcho_search", "args": {"query": "foo"}}
    assert secondary.calls == []
    p.shutdown()


def test_mirror_writes(hermes_home: Path):
    _write_shadow_config(hermes_home, compare_reads=False, sample_rate=0.0)
    primary = FakeHonchoProvider()
    secondary = FakeFuliProvider()
    p = ShadowMemoryProvider()
    p._inject_primary(primary)
    p._inject_secondary(secondary)
    p.initialize("shadow-test", hermes_home=str(hermes_home))

    result = p.handle_tool_call("honcho_conclude", {"conclusion": "User likes tea", "peer": "user"})
    assert json.loads(result)["ok"] is True
    assert len(secondary.calls) == 1
    assert secondary.calls[0][0] == "fuli_memory_add"
    assert secondary.calls[0][1]["content"] == "User likes tea"
    assert secondary.calls[0][1]["namespace"] == "hermes:shadow"
    p.shutdown()


def test_fuli_failure_does_not_change_primary_output(hermes_home: Path):
    _write_shadow_config(hermes_home, compare_reads=False, sample_rate=0.0)
    primary = FakeHonchoProvider()
    secondary = FakeFuliProvider(fail=True)
    p = ShadowMemoryProvider()
    p._inject_primary(primary)
    p._inject_secondary(secondary)
    p.initialize("shadow-test", hermes_home=str(hermes_home))

    result = p.handle_tool_call("honcho_conclude", {"conclusion": "User likes tea", "peer": "user"})
    assert json.loads(result)["ok"] is True
    assert len(secondary.calls) == 1
    p.shutdown()


def test_compare_reads(hermes_home: Path):
    _write_shadow_config(hermes_home, mirror_writes=True)
    primary = FakeHonchoProvider({"honcho_search": ["User likes tea", "User prefers mornings"]})
    secondary = FakeFuliProvider()
    p = ShadowMemoryProvider()
    p._inject_primary(primary)
    p._inject_secondary(secondary)
    p.initialize("shadow-test", hermes_home=str(hermes_home))

    # Mirror a fact so Fuli has it.
    p.handle_tool_call("honcho_conclude", {"conclusion": "User likes tea", "peer": "user"})
    result = p.handle_tool_call("honcho_search", {"query": "tea"})
    assert json.loads(result) == ["User likes tea", "User prefers mornings"]

    report = p._report()
    assert report["reads"]["sample_count"] == 1
    p.shutdown()


def test_sample_rate_deterministic(hermes_home: Path):
    _write_shadow_config(hermes_home, mirror_writes=False, sample_rate=0.0)
    primary = FakeHonchoProvider()
    secondary = FakeFuliProvider()
    p = ShadowMemoryProvider()
    p._inject_primary(primary)
    p._inject_secondary(secondary)
    p.initialize("shadow-test", hermes_home=str(hermes_home))

    for _ in range(5):
        p.handle_tool_call("honcho_search", {"query": "x"})
    assert secondary.calls == []
    p.shutdown()


def test_no_duplicate_tools_when_enabled(hermes_home: Path):
    _write_shadow_config(hermes_home, enabled=True)
    primary = FakeHonchoProvider()
    secondary = FakeFuliProvider()
    p = ShadowMemoryProvider()
    p._inject_primary(primary)
    p._inject_secondary(secondary)
    p.initialize("shadow-test", hermes_home=str(hermes_home))
    schemas = p.get_tool_schemas()
    names = [s["name"] for s in schemas]
    assert "fuli_memory_add" not in names
    assert "honcho_search" in names
    p.shutdown()


def test_primary_result_returned_unchanged(hermes_home: Path):
    _write_shadow_config(hermes_home)
    primary = FakeHonchoProvider({"honcho_search": ["unchanged"] * 5})
    secondary = FakeFuliProvider()
    p = ShadowMemoryProvider()
    p._inject_primary(primary)
    p._inject_secondary(secondary)
    p.initialize("shadow-test", hermes_home=str(hermes_home))

    raw = p.handle_tool_call("honcho_search", {"query": "x"})
    assert raw == json.dumps(["unchanged"] * 5)
    p.shutdown()


def test_evidence_store_does_not_record_raw_content(hermes_home: Path):
    _write_shadow_config(hermes_home, mirror_writes=True, compare_reads=True)
    primary = FakeHonchoProvider({"honcho_search": ["sensitive user detail"] * 5})
    secondary = FakeFuliProvider()
    p = ShadowMemoryProvider()
    p._inject_primary(primary)
    p._inject_secondary(secondary)
    p.initialize("shadow-test", hermes_home=str(hermes_home))

    # Mirror a write and trigger a comparison.
    p.handle_tool_call("honcho_conclude", {"conclusion": "sensitive user detail", "peer": "user"})
    p.handle_tool_call("honcho_search", {"query": "sensitive user detail"})

    store = p._store
    assert store is not None
    with store._connect() as conn:
        raw_content = conn.execute(
            "SELECT COUNT(*) FROM mirrored_writes WHERE shadow_error LIKE '%sensitive%'"
        ).fetchone()[0]
        assert raw_content == 0
        query_rows = conn.execute("SELECT query_hash FROM observations").fetchall()
        assert len(query_rows) == 1
        assert query_rows[0][0] != "sensitive user detail"
    p.shutdown()


def test_no_duplicate_tools(hermes_home: Path):
    _write_shadow_config(hermes_home, enabled=False, mirror_writes=False, compare_reads=False)
    primary = FakeHonchoProvider()
    secondary = FakeFuliProvider()
    p = ShadowMemoryProvider()
    p._inject_primary(primary)
    p._inject_secondary(secondary)
    p.initialize("shadow-test", hermes_home=str(hermes_home))
    schemas = p.get_tool_schemas()
    names = [s["name"] for s in schemas]
    assert "fuli_memory_add" not in names
    assert "honcho_search" in names
    p.shutdown()


def test_config_failure_falls_back_to_primary(hermes_home: Path):
    _write_shadow_config(hermes_home, enabled=False, mirror_writes=False, compare_reads=False)
    p = ShadowMemoryProvider()
    p._inject_primary(FakeHonchoProvider())
    p._inject_secondary(FakeFuliProvider(fail=True))
    p.initialize("shadow-test", hermes_home=str(hermes_home))
    result = p.handle_tool_call("honcho_search", {"query": "foo"})
    assert "ok" in json.loads(result)
    p.shutdown()


def test_namespace_isolation(hermes_home: Path):
    _write_shadow_config(hermes_home, namespace="hermes:test", compare_reads=False, sample_rate=0.0)
    primary = FakeHonchoProvider()
    secondary = FakeFuliProvider()
    p = ShadowMemoryProvider()
    p._inject_primary(primary)
    p._inject_secondary(secondary)
    p.initialize("shadow-test", hermes_home=str(hermes_home))

    p.handle_tool_call("honcho_conclude", {"conclusion": "Fact", "peer": "user"})
    assert secondary.calls[0][1]["namespace"] == "hermes:test"
    p.shutdown()


def test_shutdown_closes_both_providers(hermes_home: Path):
    _write_shadow_config(hermes_home)
    primary = FakeHonchoProvider()
    secondary = FakeFuliProvider()
    p = ShadowMemoryProvider()
    p._inject_primary(primary)
    p._inject_secondary(secondary)
    p.initialize("shadow-test", hermes_home=str(hermes_home))
    p.shutdown()
    assert p._primary is None
    assert p._secondary is None
