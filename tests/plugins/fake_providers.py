from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider


class FakeHonchoProvider(MemoryProvider):
    """Deterministic fake Honcho for shadow-provider tests.

    Simulates success, failure, timeout, initialization, validation, and parser
    error responses. Records every call for later inspection.
    """

    def __init__(self, mode: str = "success"):
        self.mode = mode
        self.calls: List[Dict[str, Any]] = []
        self.call_count = 0

    @property
    def name(self) -> str:
        return "fake_honcho"

    def is_available(self) -> bool:
        return True

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return []

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        self.call_count += 1
        result: str
        if self.mode == "success":
            result = json.dumps({"status": "success", "id": f"honcho_{self.call_count}"})
        elif self.mode == "error":
            result = json.dumps({"error": "connection refused"})
        elif self.mode == "timeout":
            result = json.dumps({"status": "error", "message": "timeout waiting for honcho"})
        elif self.mode == "initializing":
            result = json.dumps({"status": "initializing", "message": "provider not ready"})
        elif self.mode == "validation":
            result = json.dumps({"status": "rejected", "message": "invalid payload"})
        elif self.mode == "malformed":
            result = json.dumps({"foo": "bar"})
        elif self.mode == "parser_error":
            result = "not json"
        else:
            raise ValueError(f"unknown mode: {self.mode}")
        self.calls.append({"tool_name": tool_name, "args": args, "kwargs": kwargs, "result": result})
        return result


class FakeFuliProvider(MemoryProvider):
    """Deterministic fake Fuli that records adds and supports status lookup."""

    def __init__(self, failure_rate: float = 0.0, pending_rate: float = 0.0, slow_add_seconds: float = 0.0, slow_first_n: int = 0):
        self.failure_rate = failure_rate
        self.pending_rate = pending_rate
        self.slow_add_seconds = slow_add_seconds
        self.slow_first_n = slow_first_n
        self.memories: Dict[str, Dict[str, Any]] = {}
        self.add_calls: List[Dict[str, Any]] = []
        self.search_calls: List[Dict[str, Any]] = []
        self.next_id = 1

    @property
    def name(self) -> str:
        return "fake_fuli"

    def is_available(self) -> bool:
        return True

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return []

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        if tool_name == "fuli_memory_add":
            timeout_ms = args.get("timeout_ms", 10000)
            if self.slow_add_seconds and len(self.add_calls) < self.slow_first_n:
                if self.slow_add_seconds * 1000 > timeout_ms:
                    raise TimeoutError(f"Fuli add timed out after {timeout_ms}ms")
            self.add_calls.append(args)
            key = f"fuli_{self.next_id}"
            self.next_id += 1
            if self.failure_rate and len(self.memories) % 10 < int(self.failure_rate * 10):
                return json.dumps({"status": "failed", "error": "fuli rejected"})
            if self.pending_rate and len(self.memories) % 10 < int(self.pending_rate * 10):
                self.memories[key] = {**args, "status": "pending"}
                return json.dumps({"memory_id": key, "status": "pending"})
            self.memories[key] = {**args, "status": "indexed"}
            return json.dumps({"memory_id": key, "status": "indexed"})
        if tool_name == "fuli_memory_search":
            self.search_calls.append(args)
            timeout_ms = args.get("timeout_ms", 10000)
            if self.slow_add_seconds and len(self.search_calls) <= self.slow_first_n:
                if self.slow_add_seconds * 1000 > timeout_ms:
                    raise TimeoutError(f"Fuli search timed out after {timeout_ms}ms")
            return json.dumps({"results": []})
        if tool_name == "fuli_memory_diagnostics":
            return json.dumps({
                "total_memories": len(self.memories),
                "indexed_count": sum(1 for m in self.memories.values() if m["status"] == "indexed"),
                "pending_count": sum(1 for m in self.memories.values() if m["status"] == "pending"),
                "failed_count": sum(1 for m in self.memories.values() if m["status"] == "failed"),
            })
        return json.dumps({"error": "unknown tool"})

    def set_indexed(self, memory_id: str) -> None:
        if memory_id in self.memories:
            self.memories[memory_id]["status"] = "indexed"

    def batch_status(self, memory_ids: List[str]) -> Dict[str, str]:
        return {mid: self.memories.get(mid, {}).get("status", "missing") for mid in memory_ids}
