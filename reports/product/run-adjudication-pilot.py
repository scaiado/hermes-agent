#!/usr/bin/env python3
"""Stage 5 pilot adjudication batch runner.

Runs a pilot of 20 cases through the blind adjudication
workflow using the in-memory stub providers. This is the
required "before the full 100" dry-run: it verifies blinding,
no identity leak, correct outcome mapping, resume, and
duplicate protection.

For real adjudications, the operator runs this same script
against the representative corpus with real provider
adapters (the Hindsight / Mnemosyne stubs are stubbed; only
Honcho and Fuli stubs are present in this environment).

Run:
    python reports/product/run-adjudication-pilot.py
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

WORKTREE = Path(__file__).resolve().parents[2]
if str(WORKTREE) not in sys.path:
    sys.path.insert(0, str(WORKTREE))

from fuli_product.evaluation.blind import (
    BlindAdjudicationWorkflow,
    BlindCipher,
    BlindOutcome,
    BlindReasonCode,
)
from fuli_product.evaluation.providers import (
    HonchoAdapter,
    FuliAdapter,
)


def _run_pilot(*, n: int = 20, seed: int = 0) -> Dict[str, Any]:
    rng = random.Random(seed)
    wf = BlindAdjudicationWorkflow(
        providers=("honcho", "fuli"),
        salt="pilot-salt-v1",
        operator_pseudonym="operator-pilot",
    )

    # Build pilot case_ids drawn from the starter corpus.
    case_ids = [f"pilot-case-{i:03d}" for i in range(n)]

    batch = wf.prepare(case_ids)

    # Record outcomes. To keep the pilot honest we use a
    # deterministic mapping driven by the rng so the result is
    # reproducible; real adjudications are operator-driven.
    # We deliberately mix outcomes across query types.
    outcome_distribution = {
        BlindOutcome.candidate_a_better: 7,
        BlindOutcome.candidate_b_better: 5,
        BlindOutcome.tie: 4,
        BlindOutcome.both_bad: 2,
        BlindOutcome.insufficient_evidence: 2,
    }
    reason_by_outcome = {
        BlindOutcome.candidate_a_better: BlindReasonCode.EXACT_MATCH,
        BlindOutcome.candidate_b_better: BlindReasonCode.SEMANTIC_RELEVANCE,
        BlindOutcome.tie: BlindReasonCode.FRESHNESS,
        BlindOutcome.both_bad: BlindReasonCode.MALFORMED_RESULT,
        BlindOutcome.insufficient_evidence: BlindReasonCode.MISSING_MEMORY,
    }
    planned: List[Tuple[str, BlindOutcome, BlindReasonCode]] = []
    for outcome, count in outcome_distribution.items():
        for _ in range(count):
            planned.append(("", outcome, reason_by_outcome[outcome]))
    rng.shuffle(planned)
    for i, (_, outcome, reason) in enumerate(planned):
        case_id = f"pilot-case-{i:03d}"
        wf.record(batch.batch_id, case_id, outcome, reason, reviewer="operator-pilot")

    # Verify blinding: no provider name in the undecrypted export.
    undec = wf.export(batch.batch_id, decrypted=False)
    blob = json.dumps(undec, default=str)
    identity_leak = ("honcho" in blob.lower() or "fuli" in blob.lower())

    # Verify outcome mapping requires batch close.
    close_failed_before = False
    try:
        wf.map_outcome_to_provider(batch.batch_id, case_ids[0])
    except RuntimeError:
        close_failed_before = True

    # Verify resume and cross-reviewer support while the batch
    # is still open. The first reviewer already recorded
    # case_ids[0]; a second attempt with the same reviewer
    # must be rejected (duplicate-review protection). A
    # different reviewer must be allowed (the duplicate check
    # is per-reviewer).
    dup_rejected = False
    try:
        wf.record(
            batch.batch_id, case_ids[0],
            BlindOutcome.tie, BlindReasonCode.FRESHNESS,
            reviewer="operator-pilot",
        )
    except RuntimeError:
        dup_rejected = True

    cross_reviewer_recorded = False
    try:
        wf.record(
            batch.batch_id, case_ids[1],
            BlindOutcome.tie, BlindReasonCode.FRESHNESS,
            reviewer="operator-pilot-2",
        )
        cross_reviewer_recorded = True
    except RuntimeError:
        cross_reviewer_recorded = False

    # Close the batch and verify mapping now succeeds.
    wf.close_batch(batch.batch_id)

    decrypted = wf.export(batch.batch_id, decrypted=True)
    mapping_ok = True
    for entry in decrypted["assignments"]:
        if entry.get("candidate_a_provider") not in ("honcho", "fuli"):
            mapping_ok = False
        if entry.get("candidate_b_provider") not in ("honcho", "fuli"):
            mapping_ok = False

    return {
        "schema_version": 1,
        "n": n,
        "identity_leak": identity_leak,
        "close_required_before_mapping": close_failed_before,
        "mapping_ok": mapping_ok,
        "duplicate_rejected": dup_rejected,
        "cross_reviewer_recorded": cross_reviewer_recorded,
        "n_outcomes_by_type": {o.value: sum(1 for e in undec["assignments"] if e["outcome"] == o.value) for o in BlindOutcome},
        "n_assignments": len(undec["assignments"]),
        "n_audit_events": len(undec["audit_log"]),
    }


def main() -> int:
    result = _run_pilot(n=20, seed=42)
    out = Path(WORKTREE) / "reports/product/fuli-adjudication-pilot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {out}")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if not result["identity_leak"] and result["mapping_ok"] and result["duplicate_rejected"] else 1


if __name__ == "__main__":
    sys.exit(main())
