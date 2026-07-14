"""``hermes shadow`` command handler.

This module is the glue between the CLI parser and the pilot modules. It
keeps heavy imports inside each handler so ``hermes --help`` stays fast.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict


def cmd_shadow(args: argparse.Namespace) -> None:
    sub = getattr(args, "shadow_command", None)
    if sub == "preflight":
        from pilot import shadow_pilot

        result = shadow_pilot.run_preflight()
        print(json.dumps(result, indent=2))
        if not result.get("ok", False):
            sys.exit(1)
    elif sub == "status":
        from pilot import shadow_pilot

        result = shadow_pilot.run_status()
        print(json.dumps(result, indent=2))
    elif sub == "report":
        from pilot import shadow_pilot

        result = shadow_pilot.generate_cli_report()
        print(json.dumps(result, indent=2))
        if not result.get("ok", False):
            sys.exit(1)
    elif sub == "validate-report":
        report_path = getattr(args, "report_path", None)
        result = _validate_report(report_path)
        print(json.dumps(result, indent=2))
        if not result.get("ok", False):
            sys.exit(1)
    elif sub == "cleanup":
        run_id = getattr(args, "run_id", None)
        dry_run = getattr(args, "dry_run", False)
        execute = getattr(args, "execute", False)
        if not dry_run and not execute:
            print("Error: cleanup requires either --dry-run or --execute", file=sys.stderr)
            sys.exit(1)
        from pilot import shadow_pilot

        result = shadow_pilot.run_cleanup(run_id, dry_run=dry_run, execute=execute)
        print(json.dumps(result, indent=2))
    else:
        print(f"Error: unknown shadow subcommand: {sub!r}", file=sys.stderr)
        print(
            "Available: preflight, status, report, validate-report, cleanup, disagreements",
            file=sys.stderr,
        )
        sys.exit(1)


def _validate_report(report_path: str | None) -> Dict[str, Any]:
    if not report_path:
        return {"ok": False, "error": "--report-path is required"}
    p = Path(report_path)
    if not p.exists():
        return {"ok": False, "error": f"report not found: {report_path}"}
    try:
        data = json.loads(p.read_text())
    except Exception as exc:
        return {"ok": False, "error": f"failed to parse report: {exc}"}

    ledger = data.get("ledger", {})
    total = ledger.get("total_attempts", 0)
    success = ledger.get("primary_success", 0)
    failure = ledger.get("primary_failure", 0)
    mirror = ledger.get("mirror_attempted", 0)
    indexed = ledger.get("indexed", 0)
    pending = ledger.get("pending", 0)
    failed = ledger.get("failed", 0)
    errors = []
    if total != success + failure:
        errors.append(f"primary accounting: {total} != {success} + {failure}")
    if mirror != indexed + pending + failed:
        errors.append(f"mirror accounting: {mirror} != {indexed} + {pending} + {failed}")

    # Reject reports whose run_status indicates an invalid primary environment.
    # This is the post-2026-07-13 fix: a report can pass Fuli-side gates while
    # the primary was unavailable, and validate-report must surface that.
    run_status = data.get("run_status")
    if run_status and run_status != "valid":
        errors.append(
            f"run_status={run_status!r}: {data.get('status_reason') or 'primary environment invalid'}"
        )

    return {
        "ok": len(errors) == 0,
        "report_path": report_path,
        "errors": errors,
        "summary": ledger,
        "run_status": run_status,
    }
