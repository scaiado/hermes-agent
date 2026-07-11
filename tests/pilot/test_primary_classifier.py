#!/usr/bin/env python3
"""Deterministic fixture tests for primary result classification.

Captures every real response shape observed in the pilot and asserts the
classifier maps each one to the correct status.
"""

from __future__ import annotations

import pytest

from pilot.primary_classifier import (
    PrimaryStatus,
    classify_honcho_result,
)


@pytest.mark.parametrize(
    "raw, expected_status, expected_success",
    [
        # Successful conclusion/profile shapes seen from Honcho
        ('{"status": "success", "id": "abc"}', PrimaryStatus.SUCCESS, True),
        ('{"id": "abc", "created_at": "2024-01-01T00:00:00Z"}', PrimaryStatus.SUCCESS, True),
        ('{"conclusion_id": "c1", "status": "success"}', PrimaryStatus.SUCCESS, True),
        ('[{"content": "fact one"}, {"content": "fact two"}]', PrimaryStatus.SUCCESS, True),
        ('"ok"', PrimaryStatus.SUCCESS, True),
        # Errors
        ('{"error": "connection refused"}', PrimaryStatus.PROVIDER_ERROR, False),
        ('{"status": "error", "message": "timeout waiting for honcho"}', PrimaryStatus.TIMEOUT, False),
        ('{"status": "error", "message": "validation failed"}', PrimaryStatus.VALIDATION_REJECTED, False),
        ('{"status": "initializing", "message": "please wait"}', PrimaryStatus.STILL_INITIALIZING, False),
        ('{"status": "rejected", "message": "invalid peer"}', PrimaryStatus.VALIDATION_REJECTED, False),
        # Malformed / ambiguous
        ('{"foo": "bar"}', PrimaryStatus.MALFORMED_RESPONSE, False),
        ('not json', PrimaryStatus.PARSER_ERROR, False),
        ('"error: not initialized"', PrimaryStatus.STILL_INITIALIZING, False),
        ('"timeout"', PrimaryStatus.TIMEOUT, False),
    ],
)
def test_classify_honcho_result(raw, expected_status, expected_success):
    result = classify_honcho_result(raw, latency_ms=10.0)
    assert result.status == expected_status
    assert result.is_confirmed_success is expected_success
    assert result.latency_ms == 10.0


def test_classifier_preserves_raw():
    raw = '{"id": "x"}'
    result = classify_honcho_result(raw)
    assert result.raw == raw
    assert result.parsed == {"id": "x"}


def test_classifier_exception_path():
    from pilot.primary_classifier import classify_exception

    result = classify_exception(ValueError("boom"))
    assert result.status == PrimaryStatus.HARNESS_ERROR
    assert result.error_code == "harness_error"
    assert "boom" in (result.error_message or "")
