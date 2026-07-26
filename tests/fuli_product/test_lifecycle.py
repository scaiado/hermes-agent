"""Lifecycle + atomic update tests (basic state machine)."""

from __future__ import annotations

from fuli_product import FuliProductLifecycle, classify_health, ProviderHealth
from fuli_product.adapters import InMemoryConfigRepository
from fuli_product.config import FuliProductConfig, atomic_update_mode


def _repository(mode="off"):
    return InMemoryConfigRepository({
        "memory": {
            "fuli_product": {
                "mode": mode,
                "compare_reads": True,
                "sample_rate": 1.0,
            },
        },
    })


def test_load_config_off_by_default():
    repo = InMemoryConfigRepository({})
    lifecycle = FuliProductLifecycle(repo)
    assert lifecycle.cfg.mode == "off"
    assert lifecycle.cfg.schema_version == 1


def test_enable_shadow_then_disable():
    repo = _repository("off")
    lifecycle = FuliProductLifecycle(repo)
    cfg, summary = lifecycle.enable_shadow(
        primary="honcho",
        secondary="fuli",
        sample_rate=1.0,
        dry_run=False,
        fuli_available=True,
    )
    assert cfg.mode == "shadow"
    assert summary["errors"] == []
    cfg2, summary2 = lifecycle.disable(reason="test")
    assert cfg2.mode == "off"
    assert cfg2.previous_mode == "shadow"
    assert summary2["errors"] == []


def test_auto_rollback_from_hard_gate_uses_canonical_key():
    cfg = FuliProductConfig(mode="shadow", compare_reads=True, sample_rate=1.0)
    result = classify_health(
        cfg,
        primary_health=ProviderHealth(available=True),
        secondary_health=ProviderHealth(available=True),
        accounting={"primary_mutation": 1},
    )
    assert result.status == "auto_paused"
    assert "primary_mutation" in result.gates


def test_atomic_update_mode_persists():
    repo = _repository("off")
    cfg, summary = atomic_update_mode(repo, "mirror", reason="mirror test")
    assert cfg.mode == "mirror"
    assert summary["errors"] == []
    stored = repo.read_dict()["memory"]["fuli_product"]
    assert stored["mode"] == "mirror"


def test_pause_records_previous_mode():
    repo = _repository("shadow")
    lifecycle = FuliProductLifecycle(repo)
    cfg, _ = lifecycle.pause(reason="test-pause")
    assert cfg.mode == "paused"
    assert cfg.previous_mode == "shadow"


def test_rollback_restores_off():
    repo = _repository("shadow")
    lifecycle = FuliProductLifecycle(repo)
    cfg, summary = lifecycle.rollback()
    assert cfg.mode == "off"
    assert summary["action"] == "rollback"
