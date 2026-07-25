"""Versioned configuration schema, validation, and atomic migration for Fuli Memory.

This module is the single source of truth for the ``memory.fuli_product``
configuration block. It does not import Fuli or Hermes provider internals; it
only reads and writes dicts. Actual filesystem I/O is provided by an injected
``ConfigRepository`` implementation.
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Tuple, runtime_checkable

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1
CURRENT_MIGRATION_VERSION = 1

FULI_PIN_COMMIT = "727ce92603707619e0155a6c0ca1a01f5f2e07c4"

ALLOWED_MODES = {"off", "mirror", "shadow", "canary", "paused"}
ALLOWED_ROLLBACK_AUTO = {
    "primary_mutation",
    "namespace_violation",
    "raw_content_leak",
    "persistence_failure",
    "queue_drop",
    "orphaned_worker",
    "unexpected_collision",
    "sqlite_integrity_failure",
    "secondary_success_below_threshold",
}

# Map of legacy hard-gate keys (older product versions or pre-canonicalization
# accounting counters) to their canonical key. All inputs are normalized through
# this map so reports and rollback config never carry duplicate spellings.
LEGACY_HARD_GATE_KEYS: Dict[str, str] = {
    "primary_mutations": "primary_mutation",
    "namespace_violations": "namespace_violation",
    "persistence_failed": "persistence_failure",
    "queue_drops": "queue_drop",
    "dropped_queue_full": "queue_drop",
    "orphaned": "orphaned_worker",
    "secondary_success_rate_low": "secondary_success_below_threshold",
}

CANONICAL_HARD_GATE_KEYS = frozenset(ALLOWED_ROLLBACK_AUTO)
ALLOWED_QUERY_TYPES = {
    "unclassified",
    "profile",
    "preference",
    "project",
    "episodic",
    "exact",
    "semantic",
    "recent",
    "contradiction",
}


class FuliConfigError(ValueError):
    """Raised when a configuration value violates the product schema or invariants."""


@runtime_checkable
class ConfigRepository(Protocol):
    """Minimal interface for config read/write operations.

    Hermes-specific implementation is in ``fuli_product.adapters.hermes_config``.
    """

    def read_dict(self) -> Dict[str, Any]:
        ...

    def write_dict(self, data: Dict[str, Any], *, backup: bool = True, reason: str = "") -> Dict[str, Any]:
        ...


@dataclass
class ComparisonConfig:
    budget_ms: int = 2000
    workers: int = 1
    queue_size: int = 1024

    def __post_init__(self) -> None:
        self.budget_ms = max(50, min(30000, int(self.budget_ms)))
        self.workers = max(1, min(8, int(self.workers)))
        self.queue_size = max(1, min(32768, int(self.queue_size)))


@dataclass
class PrivacyConfig:
    capture_content: bool = False


@dataclass
class RollbackConfig:
    previous_provider: str = "honcho"
    auto_rollback_on: List[str] = field(default_factory=lambda: ["primary_mutation", "namespace_violation", "raw_content_leak"])

    def __post_init__(self) -> None:
        if not self.auto_rollback_on:
            self.auto_rollback_on = []
        self.auto_rollback_on = normalize_hard_gates(self.auto_rollback_on)


@dataclass
class QualityConfig:
    min_secondary_success_rate: float = 0.90
    min_adjudicated_samples: int = 100
    max_queue_drops: int = 0

    def __post_init__(self) -> None:
        self.min_secondary_success_rate = max(0.0, min(1.0, float(self.min_secondary_success_rate)))
        self.min_adjudicated_samples = max(0, int(self.min_adjudicated_samples))
        self.max_queue_drops = max(0, int(self.max_queue_drops))


@dataclass
class FuliProductConfig:
    """Canonical ``memory.fuli_product`` block."""

    schema_version: int = CURRENT_SCHEMA_VERSION
    migration_version: int = CURRENT_MIGRATION_VERSION
    mode: str = "off"
    previous_mode: str = ""
    primary_provider: str = "honcho"
    secondary_provider: str = "fuli"
    namespace: str = "hermes:default"
    mirror_writes: bool = False
    compare_reads: bool = False
    sample_rate: float = 0.0
    sampling_seed: int = 0
    comparison: ComparisonConfig = field(default_factory=ComparisonConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    rollback: RollbackConfig = field(default_factory=RollbackConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    _audit_log: List[Dict[str, str]] = field(default_factory=list)
    _unknown_fields: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.mode = _coerce_mode(self.mode)
        self.previous_mode = _coerce_mode(self.previous_mode, allow_empty=True)
        self.primary_provider = str(self.primary_provider) or "honcho"
        self.secondary_provider = str(self.secondary_provider) or "fuli"
        self.namespace = str(self.namespace) or "hermes:default"
        self.sample_rate = max(0.0, min(1.0, float(self.sample_rate)))
        self.sampling_seed = int(self.sampling_seed)
        if isinstance(self.comparison, dict):
            self.comparison = ComparisonConfig(**self.comparison)
        if isinstance(self.privacy, dict):
            self.privacy = PrivacyConfig(**self.privacy)
        if isinstance(self.rollback, dict):
            self.rollback = RollbackConfig(**self.rollback)
        if isinstance(self.quality, dict):
            self.quality = QualityConfig(**self.quality)

    def to_dict(self, preserve_unknown: bool = True) -> Dict[str, Any]:
        d = asdict(self)
        # Remove internal bookkeeping
        d.pop("_audit_log", None)
        d.pop("_unknown_fields", None)
        if preserve_unknown:
            d.update(self._unknown_fields)
        return d

    def checksum(self) -> str:
        """Stable SHA-256 of the persisted config block (no audit log)."""
        return hashlib.sha256(
            json.dumps(self.to_dict(preserve_unknown=True), sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()

    def record_transition(self, action: str, reason: str = "") -> None:
        """Append an internal audit entry. Not written to config.yaml by default."""
        import time
        self._audit_log.append({
            "action": action,
            "reason": reason,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "checksum": self.checksum(),
        })


def _coerce_mode(value: Any, allow_empty: bool = False) -> str:
    v = str(value).strip().lower() if value is not None else ""
    if allow_empty and not v:
        return ""
    if v in ALLOWED_MODES:
        return v
    raise FuliConfigError(f"Invalid Fuli product mode: {value!r}. Allowed: {sorted(ALLOWED_MODES)}")


def _extract_legacy_shadow(config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Read the legacy ``memory.shadow`` block if present."""
    memory = config.get("memory") or {}
    if not isinstance(memory, dict):
        return None
    shadow = memory.get("shadow") or {}
    if not isinstance(shadow, dict) or not shadow:
        return None
    return shadow


def normalize_hard_gates(values: Iterable[str]) -> List[str]:
    """Map any legacy hard-gate keys to their canonical names.

    Returns a deduplicated list containing only canonical keys (order preserved
    by first appearance). Unknown keys are silently dropped.
    """
    seen: Dict[str, None] = {}
    for value in values:
        key = LEGACY_HARD_GATE_KEYS.get(str(value), str(value))
        if key in CANONICAL_HARD_GATE_KEYS:
            seen.setdefault(key, None)
    return list(seen.keys())


def normalize_accounting(accounting: Dict[str, Any]) -> Dict[str, int]:
    """Return a copy of ``accounting`` with legacy hard-gate keys folded into canonical ones."""
    result: Dict[str, int] = {}
    for key, value in accounting.items():
        canonical = LEGACY_HARD_GATE_KEYS.get(str(key), str(key))
        if not isinstance(value, (int, float)):
            try:
                value = int(value)
            except Exception:
                value = 0
        result[canonical] = result.get(canonical, 0) + int(value)
    return result


def _legacy_to_product(shadow: Dict[str, Any]) -> FuliProductConfig:
    """Convert a legacy shadow config to the canonical product config."""
    enabled = bool(shadow.get("enabled", False))
    compare_reads = bool(shadow.get("compare_reads", False))
    mirror_writes = bool(shadow.get("mirror_writes", False))
    sample_rate = float(shadow.get("sample_rate", 0.0) or 0.0)

    if enabled and compare_reads and sample_rate > 0.0:
        mode = "shadow"
    elif enabled and mirror_writes:
        mode = "mirror"
    elif enabled:
        mode = "mirror"
    else:
        mode = "off"

    return FuliProductConfig(
        schema_version=CURRENT_SCHEMA_VERSION,
        migration_version=CURRENT_MIGRATION_VERSION,
        mode=mode,
        previous_mode=shadow.get("previous_mode", ""),
        primary_provider=str(shadow.get("primary_provider", "honcho")),
        secondary_provider=str(shadow.get("secondary_provider", "fuli")),
        namespace=str(shadow.get("namespace", "hermes:shadow-pilot")),
        mirror_writes=mirror_writes,
        compare_reads=compare_reads,
        sample_rate=sample_rate,
        sampling_seed=int(shadow.get("sampling_seed", 0) or 0),
        comparison=ComparisonConfig(
            budget_ms=int(shadow.get("comparison_budget_ms", 2000) or 2000),
            workers=int(shadow.get("comparison_max_workers", 1) or 1),
            queue_size=int(shadow.get("comparison_max_queue_size", 1024) or 1024),
        ),
        privacy=PrivacyConfig(
            capture_content=bool(shadow.get("capture_content", False)),
        ),
        rollback=RollbackConfig(
            previous_provider=str(shadow.get("previous_provider", "honcho")),
        ),
        quality=QualityConfig(
            min_secondary_success_rate=float(shadow.get("min_secondary_success_rate", 0.90) or 0.90),
        ),
    )


def _dict_to_config(data: Dict[str, Any]) -> FuliProductConfig:
    unknown = {k: v for k, v in data.items() if k not in _CONFIG_FIELDS}
    known = {k: v for k, v in data.items() if k in _CONFIG_FIELDS}
    cfg = FuliProductConfig(**known)
    cfg._unknown_fields = unknown
    return cfg


_CONFIG_FIELDS = {
    "schema_version", "migration_version", "mode", "previous_mode",
    "primary_provider", "secondary_provider", "namespace",
    "mirror_writes", "compare_reads", "sample_rate", "sampling_seed",
    "comparison", "privacy", "rollback", "quality",
}


def load_fuli_product_config(config: Optional[Dict[str, Any]] = None) -> FuliProductConfig:
    """Load the canonical Fuli product config from a config dict.

    If ``memory.fuli_product`` exists, it is validated and returned. If only
    the legacy ``memory.shadow`` block exists, it is migrated in-memory (no
    write). If neither exists, an OFF config is returned.
    """
    if config is None:
        return FuliProductConfig()

    memory = config.get("memory") or {}
    if not isinstance(memory, dict):
        memory = {}

    product = memory.get("fuli_product")
    if isinstance(product, dict) and product:
        return _dict_to_config(product)

    legacy = _extract_legacy_shadow(config)
    if legacy:
        cfg = _legacy_to_product(legacy)
        cfg.record_transition("legacy_migration_detected")
        return cfg

    return FuliProductConfig()


def validate_config_for_mode(
    cfg: FuliProductConfig,
    mode: str,
    *,
    fuli_available: bool = False,
    hermes_version: str = "",
) -> List[str]:
    """Return a list of errors that would prevent enabling the requested mode."""
    errors: List[str] = []
    try:
        _coerce_mode(mode)
    except FuliConfigError as exc:
        errors.append(str(exc))
        return errors

    if cfg.schema_version != CURRENT_SCHEMA_VERSION:
        errors.append(f"schema_version mismatch: {cfg.schema_version} != {CURRENT_SCHEMA_VERSION}")

    if mode in {"mirror", "shadow", "canary"}:
        if not fuli_available:
            errors.append("Fuli provider is not available; install dependencies first")
        if cfg.primary_provider == cfg.secondary_provider:
            errors.append("primary_provider and secondary_provider must be different")
        if cfg.mirror_writes is False and mode == "mirror":
            errors.append("mirror mode requires mirror_writes=true")
        if cfg.compare_reads is False and mode == "shadow":
            errors.append("shadow mode requires compare_reads=true")
        if cfg.sample_rate <= 0.0 and mode == "shadow":
            errors.append("shadow mode requires sample_rate > 0.0")
        if cfg.sample_rate <= 0.0 and mode == "canary":
            errors.append("canary mode requires sample_rate > 0.0")
        if cfg.privacy.capture_content:
            errors.append("capture_content must be false for product modes")

    if mode == "canary":
        errors.append("canary mode is not implemented in v0.1.0")

    return errors


def migrate_config_in_memory(
    config: Dict[str, Any],
    *,
    preserve_legacy: bool = True,
) -> Tuple[FuliProductConfig, Dict[str, Any]]:
    """Pure migration transform: legacy shadow -> product block.

    Returns ``(product_config, target_dict)`` where ``target_dict`` is a copy of
    ``config`` with ``memory.fuli_product`` added. The legacy block is preserved
    when ``preserve_legacy=True``. No file I/O is performed.
    """
    cfg = load_fuli_product_config(config)
    target = copy.deepcopy(config)
    target.setdefault("memory", {})

    legacy = _extract_legacy_shadow(target)
    if legacy is None and target["memory"].get("fuli_product"):
        return cfg, target

    if legacy is None:
        return cfg, target

    cfg.record_transition("config_migration")
    target["memory"]["fuli_product"] = cfg.to_dict(preserve_unknown=True)
    if not preserve_legacy:
        target["memory"].pop("shadow", None)
    return cfg, target


def migrate_config(
    repository: ConfigRepository,
    *,
    dry_run: bool = True,
    backup: bool = True,
) -> Tuple[FuliProductConfig, Dict[str, Any]]:
    """Migrate legacy ``memory.shadow`` to ``memory.fuli_product`` via repository.

    Returns ``(product_config, summary)``. If ``dry_run=True``, no write is made.
    Summary contains:
      - action: "dry_run" | "migrated" | "no_op"
      - backup_path: path or None
      - old_sha256: str
      - new_sha256: str
      - errors: list[str]
    """
    summary = {
        "action": "dry_run" if dry_run else "migrated",
        "backup_path": None,
        "old_sha256": None,
        "new_sha256": None,
        "errors": [],
    }

    config = repository.read_dict()
    raw_text = json.dumps(config, sort_keys=True, default=str)
    summary["old_sha256"] = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()

    cfg, target = migrate_config_in_memory(config, preserve_legacy=True)
    legacy = _extract_legacy_shadow(config)
    if legacy is None and config.get("memory", {}).get("fuli_product"):
        summary["action"] = "no_op"
        return cfg, summary
    if legacy is None:
        summary["action"] = "no_op"
        return cfg, summary

    if not dry_run:
        write_summary = repository.write_dict(target, backup=backup, reason="Fuli product config migration")
        summary["backup_path"] = write_summary.get("backup_path")
        summary["new_sha256"] = write_summary.get("new_sha256")

    return cfg, summary


def atomic_update_mode(
    repository: ConfigRepository,
    mode: str,
    *,
    previous_mode: Optional[str] = None,
    reason: str = "",
) -> Tuple[FuliProductConfig, Dict[str, Any]]:
    """Atomically update ``memory.fuli_product.mode`` via repository.

    Returns the updated config and a summary dict.
    """
    config = repository.read_dict()
    cfg = load_fuli_product_config(config)
    cfg.mode = _coerce_mode(mode)
    if previous_mode is not None:
        cfg.previous_mode = _coerce_mode(previous_mode, allow_empty=True)
    cfg.record_transition("mode_update", reason)

    config.setdefault("memory", {})
    config["memory"]["fuli_product"] = cfg.to_dict(preserve_unknown=True)
    write_summary = repository.write_dict(config, backup=True, reason=f"Fuli mode update to {mode}")
    return cfg, {
        "action": "mode_update",
        "old_mode": cfg.previous_mode,
        "new_mode": mode,
        "backup_path": write_summary.get("backup_path"),
        "errors": write_summary.get("errors", []),
    }
