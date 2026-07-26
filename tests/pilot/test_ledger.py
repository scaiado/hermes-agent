from __future__ import annotations

import pytest

from pilot.ledger import PilotLedger, PrimaryStatus
from pilot.primary_classifier import classify_honcho_result


@pytest.fixture
def tmp_ledger(tmp_path):
    return PilotLedger(tmp_path / "ledger.db", run_id="run-1")


def test_balanced_accounting(tmp_ledger):
    ledger = tmp_ledger
    for i in range(5):
        corr = ledger.start_operation(i, "conclude", f"fp-{i}")
        ledger.record_primary_result(corr, classify_honcho_result('{"id": "x"}'))
        ledger.record_mirror_result(corr, memory_id=f"m-{i}", accepted=True, indexed=True)
    report = ledger.compute_report()
    assert report["balanced"] is True
    assert report["total_attempts"] == 5
    assert report["primary_success"] == 5
    assert report["mirror_attempted"] == 5
    assert report["indexed"] == 5
    assert report["unexplained_mirror_attempts"] == 0


def test_unbalanced_due_to_missing_classification(tmp_ledger):
    ledger = tmp_ledger
    corr = ledger.start_operation(0, "conclude", "fp-0")
    ledger.record_primary_result(corr, classify_honcho_result('{"id": "x"}'))
    ledger.record_mirror_result(corr, memory_id="m-0", accepted=True, indexed=False, pending=False, failed=False)
    report = ledger.compute_report()
    assert report["balanced"] is False
    assert report["unexplained_mirror_attempts"] == 1


def test_primary_failure_no_mirror(tmp_ledger):
    ledger = tmp_ledger
    corr = ledger.start_operation(0, "conclude", "fp-0")
    ledger.record_primary_result(corr, classify_honcho_result('{"error": "timeout"}'))
    report = ledger.compute_report()
    assert report["balanced"] is True
    assert report["primary_success"] == 0
    assert report["primary_failure"] == 1
    assert report["mirror_attempted"] == 0


def test_report_rejects_invariant_failure(tmp_ledger):
    ledger = tmp_ledger
    corr = ledger.start_operation(0, "conclude", "fp-0")
    # Simulate a corrupted state: primary success but mirror outcome lost.
    ledger.record_primary_result(corr, classify_honcho_result('{"id": "x"}'))
    ledger.record_mirror_result(corr, memory_id=None, accepted=False, indexed=False, pending=False, failed=False)
    report = ledger.compute_report()
    assert report["balanced"] is False
