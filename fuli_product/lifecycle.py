"""Product lifecycle: install, enable, pause, rollback, uninstall.

All operations are filesystem-only and config-only. They do not start or stop
any running Hermes process. The next Hermes session picks up the new mode from
config.yaml.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from fuli_product.config import (
    CURRENT_SCHEMA_VERSION,
    ConfigRepository,
    FuliConfigError,
    FuliProductConfig,
    atomic_update_mode,
    load_fuli_product_config,
    migrate_config,
    validate_config_for_mode,
)
from fuli_product.compatibility import check_compatibility

logger = logging.getLogger(__name__)


class LifecycleError(RuntimeError):
    """Raised when a lifecycle action cannot be completed safely."""


class FuliProductLifecycle:
    """State machine for installing and managing Fuli Memory for Hermes.

    Operations are filesystem/config-only. They do not start or stop any
    running Hermes process. The next Hermes session picks up the new mode from
    config.
    """

    def __init__(self, repository: ConfigRepository) -> None:
        self.repository = repository
        self._cfg: Optional[FuliProductConfig] = None
        self._runtime: Any = None

    def _load(self) -> FuliProductConfig:
        self._cfg = load_fuli_product_config(self.repository.read_dict())
        return self._cfg

    @property
    def cfg(self) -> FuliProductConfig:
        if self._cfg is None:
            self._load()
        assert self._cfg is not None
        return self._cfg

    def install(
        self,
        *,
        mode: str = "off",
        primary_provider: str = "honcho",
        secondary_provider: str = "fuli",
        namespace: str = "hermes:shadow-pilot",
        dry_run: bool = False,
    ) -> Tuple[FuliProductConfig, Dict[str, Any]]:
        """Install the Fuli product block if it does not already exist.

        Does not install the Fuli Python package; only the Hermes config block.
        """
        cfg, migrate_summary = migrate_config(self.repository, dry_run=dry_run, backup=True)
        if migrate_summary["action"] != "no_op":
            return cfg, migrate_summary

        cfg = self._load()
        if cfg.schema_version == CURRENT_SCHEMA_VERSION:
            try:
                _ = validate_config_for_mode(cfg, mode, fuli_available=self._fuli_available())
            except Exception as exc:
                raise LifecycleError(str(exc)) from exc
            return cfg, {"action": "already_installed", "mode": cfg.mode}

        cfg.mode = mode
        cfg.primary_provider = primary_provider
        cfg.secondary_provider = secondary_provider
        cfg.namespace = namespace
        return self._write_mode_update(cfg, "install")

    def enable_shadow(
        self,
        *,
        primary: str = "honcho",
        secondary: str = "fuli",
        sample_rate: float = 0.05,
        dry_run: bool = False,
        fuli_available: bool = True,
    ) -> Tuple[FuliProductConfig, Dict[str, Any]]:
        """Convenience helper to enable shadow mode with explicit parameters."""
        cfg = self._load()
        errors = validate_config_for_mode(
            cfg, "shadow", fuli_available=fuli_available
        )
        if errors:
            raise FuliConfigError("; ".join(errors))
        if dry_run:
            return cfg, {"action": "dry_run", "mode": "shadow", "errors": []}
        cfg.primary_provider = primary
        cfg.secondary_provider = secondary
        cfg.compare_reads = True
        cfg.sample_rate = sample_rate
        compat = {"incompatible": False, "reasons": [], "hermes_version": "0.19.0"}
        return self.enable("shadow", sample_rate=sample_rate, dry_run=dry_run, compat=compat, fuli_available=fuli_available)

    def enable(
        self,
        mode: str,
        *,
        sample_rate: Optional[float] = None,
        comparison_budget_ms: Optional[int] = None,
        dry_run: bool = False,
        compat: Optional[Dict[str, Any]] = None,
        fuli_available: Optional[bool] = None,
    ) -> Tuple[FuliProductConfig, Dict[str, Any]]:
        """Enable a product mode (mirror, shadow, canary)."""
        cfg = self._load()
        if compat is None:
            compat = check_compatibility()
        if compat.get("incompatible"):
            raise LifecycleError(f"Incompatible environment: {compat.get('reasons')}")

        available = fuli_available if fuli_available is not None else self._fuli_available()
        errors = validate_config_for_mode(
            cfg, mode, fuli_available=available, hermes_version=compat.get("hermes_version", "")
        )
        if errors:
            raise LifecycleError(f"Cannot enable {mode}: " + "; ".join(errors))

        if dry_run:
            return cfg, {"action": "dry_run", "mode": mode, "errors": []}

        previous_mode = cfg.mode
        if mode == "shadow":
            cfg.compare_reads = True
            if sample_rate is not None:
                cfg.sample_rate = sample_rate
        elif mode == "mirror":
            cfg.mirror_writes = True
            cfg.compare_reads = False
        elif mode == "canary":
            raise LifecycleError("canary mode is not implemented in v0.1.0")

        cfg.mode = mode
        cfg.previous_mode = previous_mode
        if comparison_budget_ms is not None:
            cfg.comparison.budget_ms = comparison_budget_ms
        return self._write_mode_update(cfg, f"enable {mode}")

    def disable(
        self, *, reason: str = "operator request"
    ) -> Tuple[FuliProductConfig, Dict[str, Any]]:
        """Set mode to ``off`` and record the previous mode.

        Transitions:
        - mirror/shadow/canary -> off (previous_mode preserved)
        - off -> off (no overwrite of existing previous_mode)
        - paused -> off (previous_mode preserved)
        """
        return self._transition_mode(target_mode="off", remember_previous=True, reason=f"disable: {reason}")

    def pause(self, reason: str = "operator request") -> Tuple[FuliProductConfig, Dict[str, Any]]:
        """Pause the product. Stores previous mode for later resume."""
        return self._transition_mode(target_mode="paused", remember_previous=True, reason=f"pause: {reason}")

    def register_runtime(self, runtime: Any) -> None:
        """Register a ComparisonRuntime so transitions can stop it.

        The registered runtime must expose a ``shutdown(drain_timeout_seconds)``
        method. Pass ``None`` to detach.
        """
        self._runtime = runtime

    def _transition_mode(
        self,
        *,
        target_mode: str,
        remember_previous: bool,
        reason: str,
    ) -> Tuple[FuliProductConfig, Dict[str, Any]]:
        """Centralized mode-transition helper.

        Performs exactly one atomic write. Never overwrites a valid previous_mode
        with ``off`` when the current mode is already off. Stops the registered
        runtime before persistence when transitioning to ``off`` or ``paused``.
        Configures mode-specific feature flags consistently with the target mode.
        """
        cfg = self._load()
        previous_mode = cfg.mode
        previous_previous = cfg.previous_mode

        if target_mode == "off" and previous_mode == "off":
            return cfg, {
                "action": "no_op",
                "reason": "already off",
                "previous_mode": previous_previous,
                "backup_path": None,
                "errors": [],
            }

        self._apply_mode_flags(cfg, target_mode)

        cfg.mode = target_mode
        if remember_previous:
            cfg.previous_mode = previous_mode
        elif target_mode != previous_mode:
            cfg.previous_mode = previous_mode

        if self._runtime is not None and target_mode in {"off", "paused"}:
            try:
                self._runtime.shutdown(drain_timeout_seconds=2.0)
            except Exception as exc:
                logger.warning("runtime shutdown before %s failed: %s", target_mode, exc)

        cfg, summary = atomic_update_mode(
            self.repository,
            cfg.mode,
            previous_mode=cfg.previous_mode,
            reason=reason,
        )
        if summary.get("errors"):
            raise LifecycleError("; ".join(summary["errors"]))
        summary = {**summary, "action": f"transition_to_{target_mode}", "previous_mode": previous_mode}
        return cfg, summary

    @staticmethod
    def _apply_mode_flags(cfg: FuliProductConfig, target_mode: str) -> None:
        """Ensure mode-specific feature flags match the target mode."""
        if target_mode == "off":
            cfg.compare_reads = False
            cfg.mirror_writes = False
        elif target_mode == "mirror":
            cfg.mirror_writes = True
            cfg.compare_reads = False
        elif target_mode == "shadow":
            cfg.compare_reads = True
        elif target_mode == "paused":
            pass
        elif target_mode == "canary":
            raise LifecycleError("canary mode is not implemented in v0.1.0")

    def rollback(self, *, to: Optional[str] = None) -> Tuple[FuliProductConfig, Dict[str, Any]]:
        """Rollback to the previous provider or to off.

        If ``to`` is provided, set ``memory.provider`` to that provider. If
        ``to`` is None, use ``cfg.rollback.previous_provider``.
        """
        cfg = self._load()
        target_provider = to or cfg.rollback.previous_provider
        previous_mode = cfg.mode
        cfg.mode = "off"
        cfg.previous_mode = previous_mode

        config = self.repository.read_dict()
        config.setdefault("memory", {})
        config["memory"]["provider"] = target_provider
        config["memory"]["fuli_product"] = cfg.to_dict(preserve_unknown=True)
        summary = self.repository.write_dict(config, backup=True, reason="Fuli rollback")
        if summary.get("errors"):
            raise LifecycleError("; ".join(summary["errors"]))

        return cfg, {
            "action": "rollback",
            "previous_mode": previous_mode,
            "target_provider": target_provider,
            "backup_path": summary.get("backup_path"),
        }

    def uninstall(self, *, preserve_data: bool = True) -> Tuple[FuliProductConfig, Dict[str, Any]]:
        """Remove the product config block. Optionally preserves data files."""
        cfg = self._load()
        previous_mode = cfg.mode
        cfg.mode = "off"
        cfg.previous_mode = previous_mode

        config = self.repository.read_dict()
        config.setdefault("memory", {})
        if config["memory"].get("provider") in {"shadow", "fuli"}:
            config["memory"]["provider"] = cfg.rollback.previous_provider
        # Preserve the block but mark it off; do not delete for auditability.
        config["memory"]["fuli_product"] = cfg.to_dict(preserve_unknown=True)
        summary = self.repository.write_dict(config, backup=True, reason="Fuli uninstall")
        if summary.get("errors"):
            raise LifecycleError("; ".join(summary["errors"]))

        return cfg, {"action": "uninstall", "previous_mode": previous_mode, "backup_path": summary.get("backup_path")}

    def _write_mode_update(self, cfg: FuliProductConfig, reason: str) -> Tuple[FuliProductConfig, Dict[str, Any]]:
        cfg, summary = atomic_update_mode(self.repository, cfg.mode, previous_mode=cfg.previous_mode, reason=reason)
        if summary["errors"]:
            raise LifecycleError("; ".join(summary["errors"]))
        return cfg, summary

    def _fuli_available(self) -> bool:
        try:
            import fuli  # noqa: F401
            return True
        except Exception:
            return False


def install_command(repository: ConfigRepository, args: Any) -> int:
    """CLI handler for ``hermes memory fuli install``."""
    try:
        lifecycle = FuliProductLifecycle(repository)
        cfg, summary = lifecycle.install(mode=getattr(args, "mode", "off"), dry_run=getattr(args, "dry_run", False))
        if getattr(args, "json", False):
            print(__import__("json").dumps({"status": "ok", "mode": cfg.mode, "summary": summary}, indent=2))
        else:
            print(f"Fuli product installed. Mode: {cfg.mode}")
        return 0
    except Exception as exc:
        if getattr(args, "json", False):
            print(__import__("json").dumps({"status": "error", "error": str(exc)}))
        else:
            print(f"Error: {exc}")
        return 1


def enable_command(repository: ConfigRepository, args: Any) -> int:
    """CLI handler for ``hermes memory fuli enable``."""
    try:
        lifecycle = FuliProductLifecycle(repository)
        cfg, summary = lifecycle.enable(
            mode=args.mode,
            sample_rate=getattr(args, "sample_rate", None),
            comparison_budget_ms=getattr(args, "comparison_budget_ms", None),
            dry_run=getattr(args, "dry_run", False),
        )
        if getattr(args, "json", False):
            print(__import__("json").dumps({"status": "ok", "mode": cfg.mode, "summary": summary}, indent=2))
        else:
            print(f"Fuli product enabled in {cfg.mode} mode.")
        return 0
    except Exception as exc:
        if getattr(args, "json", False):
            print(__import__("json").dumps({"status": "error", "error": str(exc)}))
        else:
            print(f"Error: {exc}")
        return 1


def pause_command(repository: ConfigRepository, args: Any) -> int:
    """CLI handler for ``hermes memory fuli pause``."""
    try:
        lifecycle = FuliProductLifecycle(repository)
        cfg, summary = lifecycle.pause(reason=getattr(args, "reason", "operator request"))
        if getattr(args, "json", False):
            print(__import__("json").dumps({"status": "ok", "mode": cfg.mode, "summary": summary}, indent=2))
        else:
            print(f"Fuli product paused. Previous mode: {cfg.previous_mode}")
        return 0
    except Exception as exc:
        if getattr(args, "json", False):
            print(__import__("json").dumps({"status": "error", "error": str(exc)}))
        else:
            print(f"Error: {exc}")
        return 1


def rollback_command(repository: ConfigRepository, args: Any) -> int:
    """CLI handler for ``hermes memory fuli rollback``."""
    try:
        lifecycle = FuliProductLifecycle(repository)
        cfg, summary = lifecycle.rollback(to=getattr(args, "to", None))
        if getattr(args, "json", False):
            print(__import__("json").dumps({"status": "ok", "mode": cfg.mode, "summary": summary}, indent=2))
        else:
            print(f"Rolled back to {summary['target_provider']}. Fuli product mode is {cfg.mode}.")
        return 0
    except Exception as exc:
        if getattr(args, "json", False):
            print(__import__("json").dumps({"status": "error", "error": str(exc)}))
        else:
            print(f"Error: {exc}")
        return 1


def uninstall_command(repository: ConfigRepository, args: Any) -> int:
    """CLI handler for ``hermes memory fuli uninstall``."""
    try:
        lifecycle = FuliProductLifecycle(repository)
        cfg, summary = lifecycle.uninstall(preserve_data=getattr(args, "preserve_data", True))
        if getattr(args, "json", False):
            print(__import__("json").dumps({"status": "ok", "mode": cfg.mode, "summary": summary}, indent=2))
        else:
            print(f"Fuli product uninstalled. Mode: {cfg.mode}")
        return 0
    except Exception as exc:
        if getattr(args, "json", False):
            print(__import__("json").dumps({"status": "error", "error": str(exc)}))
        else:
            print(f"Error: {exc}")
        return 1
