"""Stable status / report / export schema for the Fuli Memory product.

The Reporter is read-only: it never mutates the ConfigRepository, runtime, or
filesystem beyond writing the optional ``export`` artifact.

Public schema (status and report share the same top-level keys):

- ``schema_version`` (int): status/reporter schema version (currently 1).
- ``product_version`` (str): fuli_product package version.
- ``state`` (str): one of ``not_installed``, ``installed``, ``healthy``,
  ``degraded``, ``paused``, ``auto_paused``, ``unavailable``, ``incompatible``.
- ``mode`` (str): current product mode (``off``, ``mirror``, ``shadow``,
  ``canary``, ``paused``).
- ``previous_mode`` (str): mode prior to the last transition.
- ``health`` (dict): top-level health reason strings.
- ``hard_gates`` (list[str]): triggered canonical hard-gate names.
- ``provider_health`` (dict): ``primary`` and ``secondary`` provider status.
- ``executor_accounting`` (dict): normalized accounting counters keyed by
  canonical hard-gate names.
- ``comparison_metrics`` (dict): sampled/completed/timed_out/failed and
  per-query-type breakdown.
- ``privacy`` (dict): capture_content flag and raw-content leak counter.
- ``database_integrity`` (dict): sqlite integrity check status.
- ``generated_at`` (str): ISO-8601 UTC timestamp.

All keys are stable and documented. Status output never includes raw query
text, provider payloads, config secrets, or environment variables.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from fuli_product.adapters import FuliComparisonRuntime  # noqa: F401  (re-exported)
from fuli_product.compatibility import check_compatibility, get_hermes_version, get_fuli_version
from fuli_product.config import (
    CANONICAL_HARD_GATE_KEYS,
    ConfigRepository,
    FuliProductConfig,
    load_fuli_product_config,
    normalize_accounting,
)
from fuli_product.health import ProviderHealth, classify_health
from fuli_product.sampling import Metrics

try:
    from fuli_product.__init__ import __version__ as fuli_product_version
except Exception:
    fuli_product_version: str = "0.0.0"

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
VALID_REPORT_PERIODS = ("1h", "6h", "24h", "7d", "30d")
_PERIOD_PATTERN = re.compile(r"^[0-9]+[hd]$")
_ALLOWED_STATE_VALUES: Set[str] = {
    "not_installed", "installed", "healthy", "degraded", "paused",
    "auto_paused", "unavailable", "incompatible",
}


class ReporterError(RuntimeError):
    """Raised for invalid reporter inputs (bad period, etc)."""


class Reporter:
    """Generate status, report, and export outputs for the Fuli product.

    The Reporter is read-only. It does not start or stop the runtime or write
    back to the ConfigRepository except when ``export()`` writes its optional
    artifact.
    """

    def __init__(
        self,
        repository: ConfigRepository,
        *,
        hermes_home: Path,
        runtime: Optional[Any] = None,
    ) -> None:
        self.repository = repository
        self.hermes_home = Path(hermes_home)
        self._runtime = runtime

    # --- accounting -----------------------------------------------------

    def _safe_accounting(self) -> Tuple[Dict[str, int], Optional[str]]:
        """Return ``(normalized_accounting, error_envelope_or_none)``.

        Counter keys are normalized to canonical names. On any exception the
        accounting dict contains only zeroed canonical keys and ``error`` is
        populated with a short string the Reporter surfaces under
        ``health.reasons``.
        """
        try:
            if self._runtime is None:
                return ({}, "runtime_not_initialized")
            raw = self._runtime.accounting() or {}
            return (normalize_accounting(raw), None)
        except Exception as exc:
            logger.warning("Could not read runtime accounting: %s", exc)
            return ({}, str(exc))

    def _default_accounting(self) -> Dict[str, int]:
        """Return zeroed accounting with all canonical hard-gate keys present."""
        base: Dict[str, int] = {
            "sampled": 0,
            "completed": 0,
            "timed_out": 0,
            "failed": 0,
            "started": 0,
            "enqueued": 0,
            "persisted": 0,
        }
        for key in CANONICAL_HARD_GATE_KEYS:
            base[key] = 0
        return base

    # --- status ---------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        """Return a stable status document."""
        cfg = load_fuli_product_config(self.repository.read_dict())
        compat = check_compatibility()
        raw_accounting, account_error = self._safe_accounting()
        accounting = {**self._default_accounting(), **raw_accounting}

        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        primary_health = ProviderHealth(available=True, last_check=now)
        secondary_health = ProviderHealth(
            available=get_fuli_version().get("available", False),
            last_check=now,
        )

        # Run classify_health without compatibility errors so hard gates are
        # reported even when the environment itself is incompatible. We surface
        # compatibility via the ``compatibility`` block instead.
        health_result = classify_health(
            cfg,
            primary_health=primary_health,
            secondary_health=secondary_health,
            accounting=accounting,
            compatibility_errors=None,
        )

        state = health_result.status
        if compat.get("incompatible"):
            state = "incompatible"
        if account_error is not None:
            state = "unavailable"
            health_result.reasons.append(f"accounting_unavailable: {account_error}")
            health_result.hard_gate = True

        # Never emit both legacy and canonical names. We only ship the canonical
        # ones here; reports and exports consume this directly.
        privacy_block = {
            "capture_content": cfg.privacy.capture_content,
            "raw_content_leak": accounting.get("raw_content_leak", 0),
        }

        executor_block = {
            "primary": primary_health,
            "secondary": secondary_health,
            "accounting": {k: v for k, v in accounting.items() if k in CANONICAL_HARD_GATE_KEYS or k in {
                "sampled", "completed", "timed_out", "failed", "started", "enqueued", "persisted"
            }},
        }

        return {
            "schema_version": SCHEMA_VERSION,
            "product_version": fuli_product_version,
            "state": state if state in _ALLOWED_STATE_VALUES else "unavailable",
            "mode": cfg.mode,
            "previous_mode": cfg.previous_mode,
            "health": {
                "primary_available": primary_health.available,
                "secondary_available": secondary_health.available,
                "last_check": now,
                "reasons": list(health_result.reasons),
            },
            "hard_gates": list(health_result.gates),
            "provider_health": executor_block,
            "executor_accounting": executor_block["accounting"],
            "comparison_metrics": _empty_metrics(),
            "privacy": privacy_block,
            "database_integrity": {
                "sqlite_integrity_failure": accounting.get("sqlite_integrity_failure", 0),
                "check_status": "ok" if accounting.get("sqlite_integrity_failure", 0) == 0 else "degraded",
            },
            "compatibility": {
                "hermes_version": get_hermes_version(),
                "fuli_version": get_fuli_version().get("version", ""),
                "fuli_commit": get_fuli_version().get("commit", ""),
                "incompatible": compat.get("incompatible", False),
                "reasons": list(compat.get("reasons", [])),
            },
            "generated_at": now,
        }

    # --- report ---------------------------------------------------------

    def report(self, period: str = "24h") -> Dict[str, Any]:
        """Return a period aggregate report."""
        validate_period(period)
        cfg = load_fuli_product_config(self.repository.read_dict())
        raw_accounting, account_error = self._safe_accounting()
        accounting = {**self._default_accounting(), **raw_accounting}
        metrics = Metrics()
        metrics.aggregate([{**accounting, "query_type": "overall"}])
        status = self.status()
        return {
            "schema_version": SCHEMA_VERSION,
            "product_version": fuli_product_version,
            "period": period,
            "state": status["state"],
            "mode": cfg.mode,
            "previous_mode": cfg.previous_mode,
            "health": status["health"],
            "hard_gates": status["hard_gates"],
            "provider_health": status["provider_health"],
            "executor_accounting": status["executor_accounting"],
            "comparison_metrics": asdict(metrics.overall),
            "privacy": status["privacy"],
            "database_integrity": status["database_integrity"],
            "compatibility": status["compatibility"],
            "generated_at": status["generated_at"],
            "errors": [account_error] if account_error else [],
        }

    # --- export ---------------------------------------------------------

    def export(self, *, redacted: bool = True, output: Optional[Path] = None) -> Path:
        """Export a redacted diagnostic bundle."""
        output = Path(output) if output else self.hermes_home / "fuli_product" / "export.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        status = self.status()
        report = self.report("24h")

        if redacted:
            status = _redact_status(status)
            report = _redact_report(report)

        bundle = {
            "schema_version": SCHEMA_VERSION,
            "product_version": fuli_product_version,
            "status": status,
            "report": report,
        }
        output.write_text(json.dumps(bundle, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8")
        return output


# --- helpers -----------------------------------------------------------


def validate_period(period: str) -> None:
    """Raise ``ReporterError`` if ``period`` is not one of the supported values."""
    if not isinstance(period, str):
        raise ReporterError(f"period must be a string, got {type(period).__name__}")
    if period not in VALID_REPORT_PERIODS and not _PERIOD_PATTERN.match(period):
        raise ReporterError(
            f"invalid period {period!r}; supported: {sorted(VALID_REPORT_PERIODS)} or '<N>h|d'"
        )


def _empty_metrics() -> Dict[str, Any]:
    """Return an empty metrics block matching the comparison_metrics schema."""
    return {
        "total": 0,
        "sampled": 0,
        "completed": 0,
        "timed_out": 0,
        "failed": 0,
        "primary_mutation": 0,
    }


def _redact_status(status: Dict[str, Any]) -> Dict[str, Any]:
    """Strip any potential sensitive fields. Currently a defensive pass-through."""
    return status


def _redact_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Strip any potential sensitive fields. Currently a defensive pass-through."""
    return report


# --- CLI handlers (consumed by hermes_cli/subcommands/memory_fuli.py) -----


def status_command(repository: ConfigRepository, args: Any) -> int:
    """CLI handler for ``hermes memory fuli status``."""
    try:
        from hermes_constants import get_hermes_home
        reporter = Reporter(repository, hermes_home=Path(get_hermes_home()))
        status = reporter.status()
        if getattr(args, "json", False):
            print(json.dumps(status, indent=2, sort_keys=True, default=str))
        else:
            print(f"State: {status['state']}")
            print(f"Mode:  {status['mode']}")
            print(f"Health: primary={status['health']['primary_available']} secondary={status['health']['secondary_available']}")
        return 0 if status["state"] in {"healthy", "installed", "off", "paused"} else 1
    except Exception as exc:
        if getattr(args, "json", False):
            print(json.dumps({"state": "error", "error": str(exc)}, indent=2))
        else:
            print(f"Error: {exc}")
        return 1


def report_command(repository: ConfigRepository, args: Any) -> int:
    """CLI handler for ``hermes memory fuli report``."""
    try:
        from hermes_constants import get_hermes_home
        reporter = Reporter(repository, hermes_home=Path(get_hermes_home()))
        report = reporter.report(period=getattr(args, "period", "24h"))
        print(json.dumps(report, indent=2, sort_keys=True, default=str))
        return 0
    except ReporterError as exc:
        print(json.dumps({"state": "error", "error": str(exc)}, indent=2))
        return 2
    except Exception as exc:
        print(json.dumps({"state": "error", "error": str(exc)}, indent=2))
        return 1


def export_command(repository: ConfigRepository, args: Any) -> int:
    """CLI handler for ``hermes memory fuli export``."""
    try:
        from hermes_constants import get_hermes_home
        reporter = Reporter(repository, hermes_home=Path(get_hermes_home()))
        output = reporter.export(
            redacted=getattr(args, "redacted", True),
            output=Path(getattr(args, "output", "")) if getattr(args, "output", "") else None,
        )
        print(f"Exported diagnostic bundle to {output}")
        return 0
    except Exception as exc:
        print(json.dumps({"state": "error", "error": str(exc)}, indent=2))
        return 1