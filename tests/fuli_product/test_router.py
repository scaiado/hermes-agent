"""Tests for the canary router."""

from __future__ import annotations

import pytest

from fuli_product.router import (
    CanaryRouter,
    Mode,
    Provider,
    RouteDecision,
)


def test_router_rejects_invalid_percentage():
    with pytest.raises(ValueError):
        CanaryRouter(canary_percentage=7)


def test_router_default_modes_route_to_honcho():
    r = CanaryRouter(canary_percentage=5)
    for m in (Mode.OFF, Mode.MIRROR, Mode.SHADOW, Mode.ADJUDICATED_SHADOW):
        d = r.route(user_id="alice", mode=m)
        assert d.provider == Provider.HONCHO


def test_router_canary_routes_fuli_for_5pct_users():
    r = CanaryRouter(canary_percentage=5, canary_salt="salt1")
    fuli_users = 0
    honcho_users = 0
    for i in range(2000):
        d = r.route(user_id=f"user-{i}", mode=Mode.CANARY)
        if d.provider == Provider.FULI:
            fuli_users += 1
        else:
            honcho_users += 1
    # 5% of 2000 = 100; allow wide tolerance
    assert 50 <= fuli_users <= 200, f"fuli_users={fuli_users}"


def test_router_assignment_is_deterministic():
    r1 = CanaryRouter(canary_percentage=5, canary_salt="salt1")
    r2 = CanaryRouter(canary_percentage=5, canary_salt="salt1")
    for i in range(100):
        d1 = r1.route(user_id=f"user-{i}", mode=Mode.CANARY)
        d2 = r2.route(user_id=f"user-{i}", mode=Mode.CANARY)
        assert d1.provider == d2.provider
        assert d1.deterministic_hash == d2.deterministic_hash


def test_router_kill_switch_routes_everyone_to_honcho():
    r = CanaryRouter(canary_percentage=20)
    r.kill()
    for i in range(50):
        d = r.route(user_id=f"user-{i}", mode=Mode.CANARY)
        assert d.provider == Provider.HONCHO
        assert d.cohort == "kill_switch"


def test_router_revive_disables_kill_switch():
    r = CanaryRouter(canary_percentage=20)
    r.kill()
    r.revive()
    d = r.route(user_id="alice", mode=Mode.CANARY)
    assert d.cohort != "kill_switch"


def test_router_allowlist_overrides_percentage():
    r = CanaryRouter(canary_percentage=1, fuli_allowlist={"vip1", "vip2"})
    d = r.route(user_id="vip1", mode=Mode.CANARY)
    assert d.provider == Provider.FULI
    assert d.cohort == "fuli"
    assert d.allowlisted is True


def test_router_honcho_allowlist_forces_honcho():
    r = CanaryRouter(canary_percentage=20, honcho_allowlist={"control"})
    d = r.route(user_id="control", mode=Mode.CANARY)
    assert d.provider == Provider.HONCHO


def test_router_health_unavailable_routes_to_honcho():
    r = CanaryRouter(canary_percentage=5)
    d = r.route(user_id="alice", mode=Mode.CANARY, health_ok=False)
    assert d.provider == Provider.HONCHO


def test_router_fuli_primary_with_fallback_returns_fallback():
    r = CanaryRouter(canary_percentage=5)
    d = r.route(user_id="alice", mode=Mode.FULI_PRIMARY_WITH_FALLBACK)
    assert d.provider == Provider.FULI
    assert d.fallback_provider == Provider.HONCHO


def test_router_returns_required_decision_fields():
    r = CanaryRouter(canary_percentage=1)
    d = r.route(user_id="alice", mode=Mode.CANARY)
    for field in (
        "provider", "fallback_provider", "cohort",
        "deterministic_hash", "user_id", "mode",
        "canary_percentage", "allowlisted",
    ):
        assert hasattr(d, field), field


def test_router_set_canary_percentage_rejects_invalid_values():
    r = CanaryRouter(canary_percentage=1)
    with pytest.raises(ValueError):
        r.set_canary_percentage(10)
    r.set_canary_percentage(20)
    assert r.canary_percentage == 20


def test_router_no_random_flapping_across_calls():
    r = CanaryRouter(canary_percentage=5, canary_salt="stable")
    for i in range(50):
        d1 = r.route(user_id=f"user-{i}", mode=Mode.CANARY)
        d2 = r.route(user_id=f"user-{i}", mode=Mode.CANARY)
        d3 = r.route(user_id=f"user-{i}", mode=Mode.CANARY)
        assert d1.cohort == d2.cohort == d3.cohort
        assert d1.provider == d2.provider == d3.provider
