"""``hermes shadow disagreements`` command handler.

Implements:
  list        show pending/decided disagreements
  show        show one comparison by comparison_id
  judge       record/update winner + optional reason code + optional note
  classify    assign query_type to a comparison
  export      emit redacted comparisons + adjudications as JSON

Privacy contract:
  - The store has no raw-content column. This module never tries to read,
    format, or print raw memory or raw query text.
  - The free-text ``--note`` is stored verbatim under operator discipline.
    Operators are responsible for keeping it free of raw content. The
    validator does not enforce that; a future audit step can.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List


def _open_store():
    """Open the comparison store at the resolved HERMES_HOME path."""
    from hermes_constants import get_hermes_home
    from pilot.comparison_store import ComparisonStore

    return ComparisonStore(Path(get_hermes_home()) / "memories" / "comparisons.db")


def _handle_list(_args: argparse.Namespace) -> int:
    store = _open_store()
    rows = store.list_comparisons(limit=200)
    out: List[Dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "comparison_id": r["comparison_id"],
                "run_id": r["run_id"],
                "timestamp": r["timestamp"],
                "namespace": r["namespace"],
                "query_hash": r["query_hash"],
                "query_type": r["query_type"],
                "primary_status": r["primary_status"],
                "secondary_status": r["secondary_status"],
                "overlap_at_1": r["overlap_at_1"],
                "overlap_at_3": r["overlap_at_3"],
                "overlap_at_5": r["overlap_at_5"],
                "reciprocal_rank_agreement": r["reciprocal_rank_agreement"],
                "adjudication_status": r["adjudication_status"],
                "content_captured": r["content_captured"],
            }
        )
    print(
        json.dumps(
            {
                "ok": True,
                "handler": "shadow_disagreements.list",
                "count": len(out),
                "comparisons": out,
            },
            indent=2,
        )
    )
    return 0


def _handle_show(args: argparse.Namespace) -> int:
    store = _open_store()
    comp = store.get_comparison(args.comparison_id)
    if comp is None:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": f"comparison_id {args.comparison_id!r} not found",
                }
            )
        )
        return 1
    adj = store.get_adjudication(args.comparison_id)
    out = {"comparison": comp}
    if adj is not None:
        out["adjudication"] = adj
    print(json.dumps({"ok": True, **out}, indent=2))
    return 0


def _handle_judge(args: argparse.Namespace) -> int:
    store = _open_store()
    try:
        result = store.record_adjudication(
            args.comparison_id,
            winner=args.winner,
            query_type="unclassified",  # classify step updates this later
            reason_code=args.reason,
            note=args.note,
            adjudicator=args.adjudicator,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "adjudication": result}, indent=2))
    return 0


def _handle_classify(args: argparse.Namespace) -> int:
    store = _open_store()
    try:
        # Re-use record_adjudication: it accepts a query_type and an
        # optional reason code, and updates the comparison row. We pass
        # the winner as 'both' (neutral) since classify only sets the
        # query_type, not a winner. This way the comparison row gains
        # its query_type while the adjudicator stays honest: an explicit
        # winner must come from ``judge``.
        result = store.record_adjudication(
            args.comparison_id,
            winner="both",
            query_type=args.query_type,
            reason_code=args.reason,
            note=None,
            adjudicator=args.adjudicator,
        )
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ok": True, "adjudication": result}, indent=2))
    return 0


def _handle_export(args: argparse.Namespace) -> int:
    store = _open_store()
    rows = store.export_redacted(limit=args.limit)
    payload = {
        "ok": True,
        "handler": "shadow_disagreements.export",
        "redacted": True,
        "count": len(rows),
        "comparisons": rows,
    }
    text = json.dumps(payload, indent=2)
    if args.output:
        Path(args.output).write_text(text)
        print(
            json.dumps(
                {
                    "ok": True,
                    "handler": "shadow_disagreements.export",
                    "written_to": str(args.output),
                    "count": len(rows),
                }
            )
        )
    else:
        print(text)
    return 0


def cmd_disagreements(args: argparse.Namespace) -> int:
    sub = getattr(args, "disagreements_command", None)
    if sub == "list":
        return _handle_list(args)
    elif sub == "show":
        return _handle_show(args)
    elif sub == "judge":
        return _handle_judge(args)
    elif sub == "classify":
        return _handle_classify(args)
    elif sub == "export":
        return _handle_export(args)
    else:
        print(
            f"Error: unknown disagreements subcommand: {sub!r}",
            file=sys.stderr,
        )
        print(
            "Available: list, show, judge, classify, export",
            file=sys.stderr,
        )
        return 1


__all__ = ["cmd_disagreements"]