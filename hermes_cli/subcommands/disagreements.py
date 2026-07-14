"""``hermes shadow disagreements`` subcommand parser.

Provides a small CLI for human adjudication of shadow comparisons:

  list        show pending and decided disagreements
  show        show one disagreement by comparison_id
  judge       record/update a winner decision
  classify    assign a query_type to a comparison
  export      dump redacted comparisons + adjudications to stdout

No raw content is ever returned. The export redacts all schema fields
that could be considered private and never emits a raw query or raw
memory string.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

# Make sure ``pilot`` resolves when this parser is imported standalone
# (e.g. from unit tests). ``hermes_cli/main.py`` already inserts the
# project root on its own path; this is a defensive bootstrap.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from pilot.comparison_store import (
    ALLOWED_QUERY_TYPES,
    ALLOWED_REASON_CODES,
    ALLOWED_WINNERS,
)


def build_disagreements_parser(subparsers, *, cmd_disagreements: Callable) -> None:
    """Attach the ``disagreements`` subcommand tree to ``subparsers``."""
    parser = subparsers.add_parser(
        "disagreements",
        help="Inspect and adjudicate shadow pilot disagreements",
        description=(
            "Human adjudication workflow for shadow pilot sampled-read "
            "comparisons. Records winners and query-type classifications "
            "without ever touching raw content."
        ),
    )
    sub = parser.add_subparsers(dest="disagreements_command")

    # list
    sub.add_parser("list", help="List comparisons (optionally filtered)")

    # show <comparison-id>
    show_parser = sub.add_parser("show", help="Show one comparison by id")
    show_parser.add_argument(
        "comparison_id",
        type=str,
        help="The comparison_id to display",
    )

    # judge <comparison-id> --winner <...>
    judge_parser = sub.add_parser(
        "judge",
        help="Record or update the winner of a comparison",
    )
    judge_parser.add_argument("comparison_id", type=str)
    judge_parser.add_argument(
        "--winner",
        required=True,
        choices=list(ALLOWED_WINNERS),
        help="Who produced the better answer",
    )
    judge_parser.add_argument(
        "--reason",
        required=False,
        choices=list(ALLOWED_REASON_CODES),
        default=None,
        help="Optional structured reason code",
    )
    judge_parser.add_argument(
        "--note",
        required=False,
        default=None,
        help=(
            "Optional free-text note. Must NOT contain raw memory or raw "
            "query text. Stored verbatim; operators are responsible for "
            "redaction discipline."
        ),
    )
    judge_parser.add_argument(
        "--adjudicator",
        required=False,
        default="cli",
        help="Who is recording this judgement (default: 'cli')",
    )

    # classify <comparison-id> --type <...>
    classify_parser = sub.add_parser(
        "classify",
        help="Assign a query_type to a comparison",
    )
    classify_parser.add_argument("comparison_id", type=str)
    classify_parser.add_argument(
        "--type",
        required=True,
        choices=list(ALLOWED_QUERY_TYPES),
        dest="query_type",
        help="The query type this comparison represents",
    )
    classify_parser.add_argument(
        "--reason",
        required=False,
        choices=list(ALLOWED_REASON_CODES),
        default=None,
        help="Optional structured reason code for the classification",
    )
    classify_parser.add_argument(
        "--adjudicator",
        required=False,
        default="cli",
        help="Who is recording this classification",
    )

    # export --redacted
    export_parser = sub.add_parser(
        "export",
        help="Dump comparisons + adjudications as JSON",
    )
    export_parser.add_argument(
        "--redacted",
        action="store_true",
        default=True,
        help=(
            "Always emit redacted output. The flag is accepted for "
            "consistency with the brief; there is no non-redacted mode."
        ),
    )
    export_parser.add_argument(
        "--output",
        required=False,
        default=None,
        help="Optional file path; default is stdout",
    )
    export_parser.add_argument(
        "--limit",
        type=int,
        default=10000,
        help="Maximum rows to export (default 10000)",
    )

    parser.set_defaults(func=cmd_disagreements)


__all__ = ["build_disagreements_parser"]