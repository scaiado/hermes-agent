"""Primary result classification for the shadow provider.

Honcho returns a variety of shapes. The classifier must look at structured
status fields first, and only fall back to string heuristics when necessary.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class PrimaryStatus(str, Enum):
    SUCCESS = "success"
    STILL_INITIALIZING = "still_initializing"
    VALIDATION_REJECTED = "validation_rejected"
    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"
    MALFORMED_RESPONSE = "malformed_response"
    PARSER_ERROR = "parser_error"
    HARNESS_ERROR = "harness_error"
    UNKNOWN = "unknown"


class PrimaryResult:
    """Structured outcome of a primary (Honcho) tool call."""

    def __init__(
        self,
        raw: str,
        status: PrimaryStatus,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        parsed: Any = None,
        latency_ms: float = 0.0,
    ) -> None:
        self.raw = raw
        self.status = status
        self.error_code = error_code
        self.error_message = error_message
        self.parsed = parsed
        self.latency_ms = latency_ms

    @property
    def is_confirmed_success(self) -> bool:
        return self.status == PrimaryStatus.SUCCESS

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "latency_ms": self.latency_ms,
        }


def classify_honcho_result(raw: str, latency_ms: float = 0.0) -> PrimaryResult:
    """Classify a raw Honcho tool-call response.

    Honcho tool responses are JSON strings. Successful writes usually return
    either:
      - a dict with a non-error status and/or an id/created_at field
      - a list (for search/context/reasoning)
      - a plain string that is not an error message

    Failures can be:
      - JSON with {"error": ...} or {"status": "error"}
      - a string containing "error", "not initialized", "timeout", etc.
      - unparseable JSON
    """
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except Exception as exc:
        return PrimaryResult(
            raw=raw,
            status=PrimaryStatus.PARSER_ERROR,
            error_code="json_parse_error",
            error_message=str(exc),
            latency_ms=latency_ms,
        )

    # Structured error objects take precedence.
    if isinstance(parsed, dict):
        status = parsed.get("status")
        error = parsed.get("error")
        if error is not None:
            return PrimaryResult(
                raw=raw,
                status=_error_status_from_message(str(error)),
                error_code=_error_code_from_message(str(error)),
                error_message=str(error),
                parsed=parsed,
                latency_ms=latency_ms,
            )
        if status == "error":
            message = parsed.get("message") or parsed.get("detail") or "unknown"
            return PrimaryResult(
                raw=raw,
                status=_error_status_from_message(str(message)),
                error_code=_error_code_from_message(str(message)),
                error_message=str(message),
                parsed=parsed,
                latency_ms=latency_ms,
            )
        if status == "initializing" or status == "not_ready":
            return PrimaryResult(
                raw=raw,
                status=PrimaryStatus.STILL_INITIALIZING,
                error_code="still_initializing",
                error_message=str(parsed.get("message") or "provider still initializing"),
                parsed=parsed,
                latency_ms=latency_ms,
            )
        if status == "rejected" or status == "validation_error":
            return PrimaryResult(
                raw=raw,
                status=PrimaryStatus.VALIDATION_REJECTED,
                error_code="validation_rejected",
                error_message=str(parsed.get("message") or "validation rejected"),
                parsed=parsed,
                latency_ms=latency_ms,
            )
        # Positive success indicators.
        if status == "success" or parsed.get("ok") is True or "result" in parsed or "id" in parsed or "memory_id" in parsed or "created_at" in parsed or "conclusion_id" in parsed:
            return PrimaryResult(
                raw=raw,
                status=PrimaryStatus.SUCCESS,
                parsed=parsed,
                latency_ms=latency_ms,
            )
        # A dict with neither success nor error markers is ambiguous; treat as
        # malformed unless it has a clear success field.
        return PrimaryResult(
            raw=raw,
            status=PrimaryStatus.MALFORMED_RESPONSE,
            error_code="malformed_response",
            error_message="response has no recognized status or id fields",
            parsed=parsed,
            latency_ms=latency_ms,
        )

    if isinstance(parsed, list):
        return PrimaryResult(
            raw=raw,
            status=PrimaryStatus.SUCCESS,
            parsed=parsed,
            latency_ms=latency_ms,
        )

    if isinstance(parsed, str):
        lower = parsed.lower()
        if any(term in lower for term in ("error", "failed", "not initialized", "timeout", "unavailable")):
            return PrimaryResult(
                raw=raw,
                status=_error_status_from_message(parsed),
                error_code=_error_code_from_message(parsed),
                error_message=parsed,
                latency_ms=latency_ms,
            )
        return PrimaryResult(
            raw=raw,
            status=PrimaryStatus.SUCCESS,
            parsed=parsed,
            latency_ms=latency_ms,
        )

    return PrimaryResult(
        raw=raw,
        status=PrimaryStatus.MALFORMED_RESPONSE,
        error_code="unexpected_json_type",
        error_message=f"unexpected JSON type: {type(parsed).__name__}",
        parsed=parsed,
        latency_ms=latency_ms,
    )


def _error_status_from_message(message: str) -> PrimaryStatus:
    lower = message.lower()
    if "timeout" in lower:
        return PrimaryStatus.TIMEOUT
    if "not initialized" in lower or "initializing" in lower:
        return PrimaryStatus.STILL_INITIALIZING
    if "validation" in lower or "invalid" in lower or "rejected" in lower:
        return PrimaryStatus.VALIDATION_REJECTED
    if "harness" in lower or "test" in lower:
        return PrimaryStatus.HARNESS_ERROR
    return PrimaryStatus.PROVIDER_ERROR


def _error_code_from_message(message: str) -> str:
    status = _error_status_from_message(message)
    return status.value


def classify_exception(exc: BaseException) -> PrimaryResult:
    return PrimaryResult(
        raw="",
        status=PrimaryStatus.HARNESS_ERROR,
        error_code="harness_error",
        error_message=f"{type(exc).__name__}: {exc}",
        latency_ms=0.0,
    )
