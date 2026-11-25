"""Typed errors for claimrev core invariants."""

from __future__ import annotations

from typing import Any


class ClaimRevError(Exception):
    """Base error with a stable machine-readable error code."""

    code: str = "ERR_CLAIMREV"

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": self.message,
            "details": self.details,
        }


class NotFoundError(ClaimRevError):
    code = "ERR_NOT_FOUND"


class ValidationError(ClaimRevError):
    code = "ERR_VALIDATION"


class MetricSemanticMismatchError(ClaimRevError):
    code = "ERR_METRIC_SEMANTIC_MISMATCH"


class UnapprovedScopeShiftError(ClaimRevError):
    code = "ERR_UNAPPROVED_SCOPE_SHIFT"


class StaleClaimVersionError(ClaimRevError):
    code = "ERR_STALE_CLAIM_VERSION"


class StaleDataBatchError(ClaimRevError):
    code = "ERR_STALE_DATA_BATCH"


class DecisionRequiredError(ClaimRevError):
    code = "ERR_DECISION_REQUIRED"


class DecisionConflictError(ClaimRevError):
    code = "ERR_DECISION_CONFLICT"


class IdempotencyConflictError(ClaimRevError):
    code = "ERR_IDEMPOTENCY_CONFLICT"


class ExportInterruptedError(ClaimRevError):
    code = "ERR_EXPORT_INTERRUPTED"
