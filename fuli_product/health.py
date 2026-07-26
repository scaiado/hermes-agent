"""Health status model, primary classifier, and hard gates.

Status values are computed from runtime accounting and health checks. They are
not process-level states. The product can be healthy even when no comparison is
in progress; it must be degraded/auto_paused when hard gates fire.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from fuli_product.config import FuliProductConfig, RollbackConfig

logger = logging.getLogger(__name__)

ALLOWED_STATUSES = {
    "not_installed",
    "installed",
    "unavailable",
    "healthy",
    "degraded",
    "paused",
    "auto_paused",
    "migration_required",
    "incompatible",
    "rollback_required",
}


class HealthError(RuntimeError):
    """Raised when a health check violates a hard gate."""


@dataclass
class ProviderHealth:
    available: bool = False
    last_error: str = ""
    last_check: str = ""
    latency_ms: float = 0.0


@dataclass
class HealthResult:
    status: str = "healthy"
    mode: str = "off"
    reasons: List[str] = field(default_factory=list)
    health: Dict[str, Any] = field(default_factory=dict)
    accounting: Dict[str, Any] = field(default_factory=dict)
    gates: List[str] = field(default_factory=list)
    hard_gate: bool = False


def classify_health(
    cfg: FuliProductConfig,
    *,
    primary_health: ProviderHealth,
    secondary_health: ProviderHealth,
    accounting: Optional[Dict[str, Any]] = None,
    compatibility_errors: Optional[List[str]] = None,
) -> HealthResult:
    """Compute the product status from config and runtime telemetry."""
    accounting = accounting or {}
    result = HealthResult(mode=cfg.mode, accounting=accounting)
    result.health = {
        "primary": primary_health,
        "secondary": secondary_health,
    }

    if compatibility_errors:
        result.status = "incompatible"
        result.reasons = compatibility_errors
        return result

    if cfg.mode == "off":
        result.status = "installed" if primary_health.available else "installed"
        return result

    if cfg.mode == "paused":
        result.status = "paused"
        result.reasons.append("Operator paused")
        return result

    if not primary_health.available:
        result.status = "degraded"
        result.reasons.append(f"Primary provider {cfg.primary_provider} is unavailable")

    if not secondary_health.available:
        result.status = "degraded"
        result.reasons.append(f"Secondary provider {cfg.secondary_provider} is unavailable")

    hard_gates = _check_hard_gates(cfg, accounting)
    if hard_gates:
        result.status = "auto_paused"
        result.reasons.extend(hard_gates)
        result.gates = hard_gates
        result.hard_gate = True

    if result.status == "healthy":
        # Check soft degradation thresholds
        success_rate = _success_rate(accounting)
        if success_rate is not None and success_rate < cfg.quality.min_secondary_success_rate:
            if _sample_count(accounting) >= 20:
                result.status = "degraded"
                result.reasons.append(
                    f"Secondary success rate {success_rate:.2%} below threshold {cfg.quality.min_secondary_success_rate:.0%}"
                )
        queue_drops = accounting.get("queue_drop", accounting.get("dropped_queue_full", 0))
        if queue_drops > cfg.quality.max_queue_drops:
            result.status = "degraded"
            result.reasons.append(f"Queue drops {queue_drops} exceed max {cfg.quality.max_queue_drops}")

    return result


def _check_hard_gates(cfg: FuliProductConfig, accounting: Dict[str, Any]) -> List[str]:
    """Return the list of triggered hard gates.

    All accounting keys are normalized through ``normalize_accounting`` so
    legacy names (``primary_mutations``, ``dropped_queue_full``, etc.) map to
    the canonical names in ``ALLOWED_ROLLBACK_AUTO``.
    """
    from fuli_product.config import normalize_accounting
    normalized = normalize_accounting(accounting)
    gates: List[str] = []
    if normalized.get("primary_mutation", 0) > 0:
        gates.append("primary_mutation")
    if normalized.get("namespace_violation", 0) > 0:
        gates.append("namespace_violation")
    if normalized.get("raw_content_leak", 0) > 0:
        gates.append("raw_content_leak")
    if normalized.get("persistence_failure", 0) > 0:
        gates.append("persistence_failure")
    if normalized.get("queue_drop", 0) > 0:
        gates.append("queue_drop")
    if normalized.get("orphaned_worker", 0) > 0:
        gates.append("orphaned_worker")
    if normalized.get("unexpected_collision", 0) > 0:
        gates.append("unexpected_collision")
    if normalized.get("sqlite_integrity_failure", 0) > 0:
        gates.append("sqlite_integrity_failure")
    success_rate = _success_rate(normalized)
    if success_rate is not None and _sample_count(normalized) >= 20 and success_rate < cfg.quality.min_secondary_success_rate:
        gates.append("secondary_success_below_threshold")

    return [g for g in gates if g in cfg.rollback.auto_rollback_on]


def _success_rate(accounting: Dict[str, Any]) -> Optional[float]:
    completed = accounting.get("completed", 0)
    started = accounting.get("started", 0)
    if started == 0:
        return None
    return completed / max(1, started)


def _sample_count(accounting: Dict[str, Any]) -> int:
    return accounting.get("started", 0)


def should_auto_rollback(result: HealthResult) -> bool:
    """Return True if the current health result requires immediate rollback."""
    return result.hard_gate and result.status in {"auto_paused", "rollback_required"}
