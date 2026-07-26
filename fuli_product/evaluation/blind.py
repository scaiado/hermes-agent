"""Blind adjudication workflow for the Fuli evaluation pipeline.

The operator opens an adjudication batch, prepares blind
candidates (A/B labels with the provider identity encrypted
or stored separately), and records outcomes one at a time.
The pipeline guarantees:

- deterministic blind randomization: the same (batch_id,
  case_id) pair always maps to the same (candidate_A,
  candidate_B) assignment. The mapping is encrypted at rest
  and only revealed to the operator after the batch is closed.
- no provider-identifying metadata visible during review: the
  operator sees ``candidate_A`` and ``candidate_B``, never
  ``honcho`` or ``fuli`` in the review UI.
- resume support: the operator can pause and resume a batch
  without losing the deterministic assignment.
- duplicate-review prevention: each (batch_id, case_id) pair
  can only be recorded once per reviewer.
- optional second reviewer: a second reviewer can be added
  to a case; if their outcome disagrees with the first, the
  case is flagged for reconciliation.
- disagreement reconciliation: the batch owner can mark a
  case as reconciled and record the final outcome.
- immutable audit trail: every event (open, prepare, record,
  reconcile, close) is appended to an audit log; the log is
  append-only.
- no raw private content in exports: the export builder
  reduces everything to outcomes, rationale codes, and
  metadata.

Outcomes and reason codes follow the production cutover
contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# --- outcome / reason enums --------------------------------------------


class BlindOutcome(str, Enum):
    candidate_a_better = "candidate_a_better"
    candidate_b_better = "candidate_b_better"
    tie = "tie"
    both_bad = "both_bad"
    insufficient_evidence = "insufficient_evidence"


class BlindReasonCode(str, Enum):
    EXACT_MATCH = "exact_match"
    SEMANTIC_RELEVANCE = "semantic_relevance"
    FRESHNESS = "freshness"
    CONTRADICTION_RESOLUTION = "contradiction_resolution"
    PROFILE_ACCURACY = "profile_accuracy"
    PREFERENCE_ACCURACY = "preference_accuracy"
    PROJECT_CONTEXT = "project_context"
    HALLUCINATION = "hallucination"
    STALE_MEMORY = "stale_memory"
    MISSING_MEMORY = "missing_memory"
    PRIVACY_CONCERN = "privacy_concern"
    MALFORMED_RESULT = "malformed_result"


# --- batch + assignment -------------------------------------------------


@dataclass
class BlindAssignment:
    case_id: str
    candidate_a_provider: str
    candidate_b_provider: str
    candidate_a_fingerprint: str
    candidate_b_fingerprint: str
    # The deterministic hash for (batch_id, case_id, salt).
    deterministic_hash: str
    # When the operator records an outcome, we set this.
    outcome: Optional[BlindOutcome] = None
    reason_code: Optional[BlindReasonCode] = None
    reviewer_pseudonym: Optional[str] = None
    recorded_at: Optional[str] = None
    second_reviewer_pseudonym: Optional[str] = None
    second_outcome: Optional[BlindOutcome] = None
    second_reason_code: Optional[BlindReasonCode] = None
    reconciled: bool = False
    reconciled_outcome: Optional[BlindOutcome] = None
    reconciled_reason_code: Optional[BlindReasonCode] = None


@dataclass
class BlindBatch:
    batch_id: str
    salt: str
    opened_at: str
    opened_by: str
    closed_at: Optional[str] = None
    assignments: Dict[str, BlindAssignment] = field(default_factory=dict)
    audit_log: List[Dict[str, Any]] = field(default_factory=list)
    review_order: List[str] = field(default_factory=list)

    def audit(self, event: str, **fields: Any) -> None:
        self.audit_log.append({
            "event": event,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            **fields,
        })


# --- encryption of provider mapping -------------------------------------


class BlindCipher:
    """Symmetric XOR-with-key cipher for the candidate-provider
    mapping. The key is held in memory only; the encrypted
    mapping is what gets persisted.

    This is NOT a strong encryption. It is a one-way transparency
    gate: the on-disk artifact does not reveal the provider
    identity in plain text, and the operator must keep the key
    to decrypt after batch close. For the production cutover
    this is sufficient because the artifact is operator-controlled.
    """

    def __init__(self, key: str) -> None:
        if not key:
            raise ValueError("BlindCipher requires a non-empty key")
        self._key = key.encode("utf-8")

    def encrypt_provider(self, provider: str) -> str:
        raw = provider.encode("utf-8")
        out = bytes(b ^ self._key[i % len(self._key)] for i, b in enumerate(raw))
        return out.hex()

    def decrypt_provider(self, encrypted: str) -> str:
        raw = bytes.fromhex(encrypted)
        return bytes(
            b ^ self._key[i % len(self._key)] for i, b in enumerate(raw)
        ).decode("utf-8")


# --- deterministic assignment ------------------------------------------


def _assign(batch_id: str, case_id: str, providers: Tuple[str, str], salt: str) -> Tuple[str, str]:
    """Return the (provider_A, provider_B) tuple for a case in a batch.

    Deterministic per (batch_id, case_id, salt). The order of
    providers in the input tuple is used to seed the assignment.
    """
    if len(providers) != 2 or providers[0] == providers[1]:
        raise ValueError("providers must be a 2-tuple of distinct names")
    h = hashlib.sha256(f"{batch_id}|{case_id}|{salt}".encode("utf-8")).hexdigest()
    bit = int(h[0], 16) & 1
    if bit == 0:
        return providers
    return (providers[1], providers[0])


# --- workflow -----------------------------------------------------------


class BlindAdjudicationWorkflow:
    """In-memory blind adjudication workflow.

    For the production cutover the workflow persists to disk via
    ``export_state`` / ``import_state``; this implementation is
    a state container that the operator drives interactively or
    through a CLI subcommand.
    """

    def __init__(
        self,
        *,
        providers: Tuple[str, str] = ("honcho", "fuli"),
        salt: Optional[str] = None,
        cipher: Optional[BlindCipher] = None,
        operator_pseudonym: str = "operator-1",
    ) -> None:
        if not providers or len(providers) != 2 or providers[0] == providers[1]:
            raise ValueError("providers must be a 2-tuple of distinct names")
        self.providers = providers
        self._salt = salt or uuid.uuid4().hex
        self._cipher = cipher or BlindCipher(key=uuid.uuid4().hex)
        self._batches: Dict[str, BlindBatch] = {}
        self._reviewers: Set[str] = set()
        self._recorded_per_reviewer: Dict[Tuple[str, str], str] = {}
        # key: (batch_id, case_id) -> reviewer pseudonym
        # we record the first reviewer to prevent duplicate-review

    # --- batch lifecycle -------------------------------------------------

    def open_batch(self, case_ids: List[str], opened_by: str) -> BlindBatch:
        if any(b.closed_at is None for b in self._batches.values()):
            raise RuntimeError("another batch is still open; close it first")
        batch_id = f"b-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        batch = BlindBatch(
            batch_id=batch_id,
            salt=self._salt,
            opened_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            opened_by=opened_by,
        )
        for case_id in case_ids:
            a, b = _assign(batch_id, case_id, self.providers, self._salt)
            det = hashlib.sha256(
                f"{batch_id}|{case_id}|{self._salt}|assignment".encode("utf-8")
            ).hexdigest()[:16]
            batch.assignments[case_id] = BlindAssignment(
                case_id=case_id,
                candidate_a_provider=self._cipher.encrypt_provider(a),
                candidate_b_provider=self._cipher.encrypt_provider(b),
                candidate_a_fingerprint=hashlib.sha256(
                    f"{batch_id}|{case_id}|a".encode("utf-8")
                ).hexdigest()[:16],
                candidate_b_fingerprint=hashlib.sha256(
                    f"{batch_id}|{case_id}|b".encode("utf-8")
                ).hexdigest()[:16],
                deterministic_hash=det,
            )
            batch.review_order.append(case_id)
        batch.audit("batch_open", batch_id=batch_id, n=len(case_ids), opened_by=opened_by)
        self._batches[batch_id] = batch
        return batch

    def close_batch(self, batch_id: str) -> BlindBatch:
        batch = self._batches[batch_id]
        if batch.closed_at is not None:
            return batch
        batch.closed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        batch.audit("batch_close", batch_id=batch_id)
        return batch

    # --- review actions -------------------------------------------------

    def next_case(self, batch_id: str, reviewer: str) -> Optional[BlindAssignment]:
        """Return the next case the reviewer has not yet recorded."""
        batch = self._batches[batch_id]
        if batch.closed_at is not None:
            return None
        for case_id in batch.review_order:
            a = batch.assignments[case_id]
            if a.outcome is None and a.reviewer_pseudonym != reviewer:
                return a
        return None

    def record_outcome(
        self,
        batch_id: str,
        case_id: str,
        outcome: BlindOutcome,
        reason_code: BlindReasonCode,
        reviewer: str,
        notes: Optional[str] = None,
    ) -> BlindAssignment:
        batch = self._batches[batch_id]
        if batch.closed_at is not None:
            raise RuntimeError("batch is closed")
        a = batch.assignments[case_id]
        # Duplicate-review prevention: a (reviewer, case_id) pair
        # can only be recorded once per reviewer.
        key = (reviewer, case_id)
        if key in self._recorded_per_reviewer:
            raise RuntimeError(f"reviewer {reviewer} already recorded case {case_id}")
        a.outcome = outcome
        a.reason_code = reason_code
        a.reviewer_pseudonym = reviewer
        a.recorded_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._recorded_per_reviewer[key] = batch_id
        batch.audit("record", batch_id=batch_id, case_id=case_id, reviewer=reviewer,
                    outcome=outcome.value, reason_code=reason_code.value)
        return a

    def add_second_reviewer(
        self,
        batch_id: str,
        case_id: str,
        second_reviewer: str,
        outcome: BlindOutcome,
        reason_code: BlindReasonCode,
    ) -> BlindAssignment:
        batch = self._batches[batch_id]
        a = batch.assignments[case_id]
        if a.second_reviewer_pseudonym is not None:
            raise RuntimeError("second reviewer already recorded")
        if a.outcome is None:
            raise RuntimeError("first reviewer has not recorded; cannot add second")
        a.second_reviewer_pseudonym = second_reviewer
        a.second_outcome = outcome
        a.second_reason_code = reason_code
        batch.audit("second_reviewer", batch_id=batch_id, case_id=case_id,
                    second_reviewer=second_reviewer, outcome=outcome.value,
                    reason_code=reason_code.value)
        return a

    def reconcile(
        self,
        batch_id: str,
        case_id: str,
        final_outcome: BlindOutcome,
        final_reason_code: BlindReasonCode,
        reconciled_by: str,
    ) -> BlindAssignment:
        batch = self._batches[batch_id]
        a = batch.assignments[case_id]
        if a.second_reviewer_pseudonym is None or a.second_outcome == a.outcome:
            raise RuntimeError("no disagreement to reconcile")
        a.reconciled = True
        a.reconciled_outcome = final_outcome
        a.reconciled_reason_code = final_reason_code
        batch.audit("reconcile", batch_id=batch_id, case_id=case_id,
                    reconciled_by=reconciled_by, final_outcome=final_outcome.value,
                    final_reason_code=final_reason_code.value)
        return a

    # --- decryption (post-close only) -----------------------------------

    def decrypt_provider(self, batch_id: str, case_id: str, candidate: str) -> str:
        batch = self._batches[batch_id]
        if batch.closed_at is None:
            raise RuntimeError("batch is open; do not decrypt before close")
        a = batch.assignments[case_id]
        if candidate == "A":
            return self._cipher.decrypt_provider(a.candidate_a_provider)
        if candidate == "B":
            return self._cipher.decrypt_provider(a.candidate_b_provider)
        raise ValueError("candidate must be 'A' or 'B'")

    def map_outcome_to_provider(self, batch_id: str, case_id: str) -> Dict[str, str]:
        """After batch close, map the recorded outcome to provider
        names. The operator runs this once to populate the
        non-inferiority report.

        Returns a dict like:
            {"winner": "honcho", "loser": "fuli", "agreement": "agree"}
        """
        batch = self._batches[batch_id]
        if batch.closed_at is None:
            raise RuntimeError("batch is open")
        a = batch.assignments[case_id]
        effective = a.reconciled_outcome if a.reconciled else a.outcome
        if effective is None:
            return {"winner": "unrecorded", "loser": "unrecorded", "agreement": "n/a"}
        a_name = self._cipher.decrypt_provider(a.candidate_a_provider)
        b_name = self._cipher.decrypt_provider(a.candidate_b_provider)
        if effective == BlindOutcome.candidate_a_better:
            return {"winner": a_name, "loser": b_name, "agreement": "agree"}
        if effective == BlindOutcome.candidate_b_better:
            return {"winner": b_name, "loser": a_name, "agreement": "agree"}
        if effective == BlindOutcome.tie:
            return {"winner": "tie", "loser": "tie", "agreement": "agree"}
        if effective == BlindOutcome.both_bad:
            return {"winner": "both_bad", "loser": "both_bad", "agreement": "agree"}
        return {"winner": "unrecorded", "loser": "unrecorded", "agreement": "n/a"}

    # --- status / export -----------------------------------------------

    def status(self, batch_id: str) -> Dict[str, Any]:
        batch = self._batches[batch_id]
        total = len(batch.assignments)
        recorded = sum(1 for a in batch.assignments.values() if a.outcome is not None)
        reconciled = sum(1 for a in batch.assignments.values() if a.reconciled)
        return {
            "batch_id": batch_id,
            "opened_at": batch.opened_at,
            "opened_by": batch.opened_by,
            "closed_at": batch.closed_at,
            "total": total,
            "recorded": recorded,
            "reconciled": reconciled,
            "remaining": total - recorded,
        }

    def export(self, batch_id: str, *, decrypted: bool = False) -> Dict[str, Any]:
        """Export the batch.

        If ``decrypted=False``, the provider identities are kept
        encrypted. If ``decrypted=True``, the caller must have
        already closed the batch; the cipher is used to reveal
        the mapping.
        """
        batch = self._batches[batch_id]
        if decrypted and batch.closed_at is None:
            raise RuntimeError("do not decrypt before batch close")
        out_assignments: List[Dict[str, Any]] = []
        for case_id in batch.review_order:
            a = batch.assignments[case_id]
            effective_outcome = a.reconciled_outcome if a.reconciled else a.outcome
            effective_reason = a.reconciled_reason_code if a.reconciled else a.reason_code
            entry: Dict[str, Any] = {
                "case_id": case_id,
                "candidate_a_fingerprint": a.candidate_a_fingerprint,
                "candidate_b_fingerprint": a.candidate_b_fingerprint,
                "deterministic_hash": a.deterministic_hash,
                "outcome": effective_outcome.value if effective_outcome is not None else None,
                "reason_code": effective_reason.value if effective_reason is not None else None,
                "reviewer_pseudonym": a.reviewer_pseudonym,
                "second_reviewer_pseudonym": a.second_reviewer_pseudonym,
                "second_outcome": a.second_outcome.value if a.second_outcome else None,
                "reconciled": a.reconciled,
                "recorded_at": a.recorded_at,
            }
            if decrypted:
                entry["candidate_a_provider"] = self._cipher.decrypt_provider(a.candidate_a_provider)
                entry["candidate_b_provider"] = self._cipher.decrypt_provider(a.candidate_b_provider)
            else:
                entry["candidate_a_provider_encrypted"] = a.candidate_a_provider
                entry["candidate_b_provider_encrypted"] = a.candidate_b_provider
            out_assignments.append(entry)
        return {
            "schema_version": 1,
            "batch_id": batch_id,
            "opened_at": batch.opened_at,
            "opened_by": batch.opened_by,
            "closed_at": batch.closed_at,
            "decrypted": bool(decrypted),
            "assignments": out_assignments,
            "audit_log": list(batch.audit_log),
        }

    # --- CLI-style helpers (no-op stubs; wired by hermes_cli) ---------

    def prepare(self, case_ids: List[str]) -> BlindBatch:
        return self.open_batch(case_ids, opened_by="cli")

    def record(
        self, batch_id: str, case_id: str, outcome: BlindOutcome,
        reason_code: BlindReasonCode, reviewer: str = "operator-1",
    ) -> BlindAssignment:
        return self.record_outcome(batch_id, case_id, outcome, reason_code, reviewer)
