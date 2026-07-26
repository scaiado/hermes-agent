"""CLI subcommand for Fuli Memory product management.

Adds ``hermes memory fuli`` subcommands. The plugin does not depend on the Fuli
package unless the user enables a product mode that uses it.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List


def add_subparser(subparsers: Any) -> None:
    """Register the ``hermes memory fuli`` subcommand tree."""
    fuli = subparsers.add_parser("fuli", help="Manage Fuli Memory for Hermes")
    fuli_sub = fuli.add_subparsers(dest="fuli_command", required=True)

    install = fuli_sub.add_parser("install", help="Install the Fuli product config")
    install.add_argument("--mode", choices=["off", "mirror", "shadow"], default="off")
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--json", action="store_true")
    install.set_defaults(func=_install)

    enable = fuli_sub.add_parser("enable", help="Enable a Fuli product mode")
    enable.add_argument("--mode", choices=["off", "mirror", "shadow", "canary"], required=True)
    enable.add_argument("--sample-rate", type=float, default=None)
    enable.add_argument("--comparison-budget-ms", type=int, default=None)
    enable.add_argument("--dry-run", action="store_true")
    enable.add_argument("--json", action="store_true")
    enable.set_defaults(func=_enable)

    pause = fuli_sub.add_parser("pause", help="Pause the Fuli product")
    pause.add_argument("--reason", default="operator request")
    pause.add_argument("--json", action="store_true")
    pause.set_defaults(func=_pause)

    status = fuli_sub.add_parser("status", help="Show Fuli product status")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=_status)

    report = fuli_sub.add_parser("report", help="Show Fuli product report")
    report.add_argument("--period", choices=["1h", "24h", "7d"], default="24h")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=_report)

    export = fuli_sub.add_parser("export", help="Export a redacted diagnostic bundle")
    export.add_argument("--redacted", action="store_true", default=True)
    export.add_argument("--output", type=Path, default=None)
    export.add_argument("--json", action="store_true")
    export.set_defaults(func=_export)

    rollback = fuli_sub.add_parser("rollback", help="Rollback to the previous provider")
    rollback.add_argument("--to", default=None)
    rollback.add_argument("--json", action="store_true")
    rollback.set_defaults(func=_rollback)

    uninstall = fuli_sub.add_parser("uninstall", help="Uninstall the Fuli product config")
    uninstall.add_argument("--preserve-data", action="store_true", default=True)
    uninstall.add_argument("--json", action="store_true")
    uninstall.set_defaults(func=_uninstall)

    doctor = fuli_sub.add_parser("doctor", help="Run diagnostic checks")
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_doctor)


def _install(args: Any) -> int:
    from fuli_product.lifecycle import install_command
    return install_command(args)


def _enable(args: Any) -> int:
    from fuli_product.lifecycle import enable_command
    return enable_command(args)


def _pause(args: Any) -> int:
    from fuli_product.lifecycle import pause_command
    return pause_command(args)


def _status(args: Any) -> int:
    from fuli_product.reporting import status_command
    return status_command(args)


def _report(args: Any) -> int:
    from fuli_product.reporting import report_command
    return report_command(args)


def _export(args: Any) -> int:
    from fuli_product.reporting import export_command
    return export_command(args)


def _rollback(args: Any) -> int:
    from fuli_product.lifecycle import rollback_command
    return rollback_command(args)


def _uninstall(args: Any) -> int:
    from fuli_product.lifecycle import uninstall_command
    return uninstall_command(args)


def _doctor(args: Any) -> int:
    from fuli_product.compatibility import check_compatibility
    from fuli_product.config import load_fuli_product_config
    import json
    cfg = load_fuli_product_config()
    compat = check_compatibility()
    result = {
        "config_mode": cfg.mode,
        "compatibility": compat,
        "schema_version": cfg.schema_version,
    }
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, default=str))
    else:
        print(json.dumps(result, indent=2, default=str))
    return 0 if not compat.get("incompatible") else 3
