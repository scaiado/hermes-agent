"""``hermes shadow`` subcommand parser.

Provides real, distinct subcommands for the Hermes ↔ Fuli shadow pilot:
  preflight, status, report, cleanup, validate-report
"""

from __future__ import annotations

import argparse
from typing import Callable


def build_shadow_parser(subparsers, *, cmd_shadow: Callable) -> None:
    """Attach the ``shadow`` subcommand to ``subparsers``."""
    shadow_parser = subparsers.add_parser(
        "shadow",
        help="Manage the Hermes ↔ Fuli shadow memory pilot",
        description=(
            "Run the shadow memory pilot and inspect its reports.\n\n"
            "The shadow provider keeps Honcho as the authoritative memory\n"
            "source while mirroring writes to Fuli for comparison."
        ),
    )
    shadow_sub = shadow_parser.add_subparsers(dest="shadow_command")
    shadow_sub.add_parser("preflight", help="Run pre-flight checks for the shadow pilot")
    shadow_sub.add_parser("status", help="Show current shadow pilot status")
    shadow_sub.add_parser("report", help="Generate the latest shadow pilot report")
    _validate_parser = shadow_sub.add_parser(
        "validate-report", help="Validate a generated report's accounting"
    )
    _validate_parser.add_argument(
        "--report-path",
        type=str,
        default=None,
        help="Path to a report JSON file to validate",
    )
    _cleanup_parser = shadow_sub.add_parser(
        "cleanup", help="Remove synthetic shadow memories and evidence"
    )
    _cleanup_parser.add_argument(
        "--run-id",
        type=str,
        required=True,
        help="Pilot run ID whose synthetic data should be removed",
    )
    _cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted without deleting anything",
    )
    _cleanup_parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform deletion",
    )
    # New disagreements subcommand tree. Lazy-imported to keep ``--help`` fast.
    from hermes_cli.subcommands.disagreements import build_disagreements_parser
    from hermes_cli.shadow_disagreements import cmd_disagreements as _cmd_disagreements

    # The disagreements subparser sets ``func=cmd_disagreements`` directly, so
    # argparse will invoke it on parse. No bridging needed.
    build_disagreements_parser(shadow_sub, cmd_disagreements=_cmd_disagreements)
    shadow_parser.set_defaults(func=cmd_shadow)
