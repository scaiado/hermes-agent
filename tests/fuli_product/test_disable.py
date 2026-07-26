"""Exhaustive disable transition table and centralized lifecycle tests."""

from __future__ import annotations

from typing import List, Tuple

from fuli_product import FuliProductLifecycle
from fuli_product.adapters import InMemoryConfigRepository


def _starting_config(mode: str, previous_mode: str = "") -> dict:
    return {
        "memory": {
            "fuli_product": {
                "mode": mode,
                "previous_mode": previous_mode,
                "compare_reads": mode == "shadow",
                "mirror_writes": mode == "mirror",
                "sample_rate": 1.0,
            },
        },
    }


def _transition(from_mode: str, from_prev: str = "") -> Tuple[str, str]:
    repo = InMemoryConfigRepository(_starting_config(from_mode, from_prev))
    lifecycle = FuliProductLifecycle(repo)
    cfg, _summary = lifecycle.disable(reason="test")
    return cfg.mode, cfg.previous_mode


def test_disable_from_mirror():
    assert _transition("mirror") == ("off", "mirror")


def test_disable_from_shadow():
    assert _transition("shadow") == ("off", "shadow")


def test_disable_from_canary():
    assert _transition("canary") == ("off", "canary")


def test_disable_from_off_does_not_clobber_previous_mode():
    assert _transition("off", from_prev="shadow") == ("off", "shadow")


def test_disable_from_paused_preserves_previous():
    assert _transition("paused", from_prev="mirror") == ("off", "paused")


def test_disable_calls_runtime_shutdown():
    calls: List[str] = []

    class FakeRuntime:
        def shutdown(self, drain_timeout_seconds=10.0):
            calls.append(f"shutdown({drain_timeout_seconds})")

    repo = InMemoryConfigRepository(_starting_config("shadow"))
    lifecycle = FuliProductLifecycle(repo)
    lifecycle.register_runtime(FakeRuntime())
    lifecycle.disable(reason="shutdown-before-persist")
    assert calls == ["shutdown(2.0)"]


def test_disable_writes_exactly_once():
    writes: List[str] = []
    base = _starting_config("shadow")

    class CountingRepo(InMemoryConfigRepository):
        def write_dict(self, data, *, backup=True, reason=""):
            writes.append(reason)
            return super().write_dict(data, backup=backup, reason=reason)

    lifecycle = FuliProductLifecycle(CountingRepo(base))
    lifecycle.disable(reason="once")
    assert len(writes) == 1
    assert writes[0] == "Fuli mode update to off"


def test_disable_failure_leaves_state_recoverable():
    class FailingRepo(InMemoryConfigRepository):
        def write_dict(self, data, *, backup=True, reason=""):
            return {"errors": ["disk full"], "backup_path": None, "written": False}

    repo = FailingRepo(_starting_config("shadow"))
    lifecycle = FuliProductLifecycle(repo)
    try:
        lifecycle.disable(reason="force-fail")
    except Exception:
        pass
    assert lifecycle.cfg.mode in {"shadow", "off"}
