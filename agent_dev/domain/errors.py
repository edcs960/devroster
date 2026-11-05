"""Stable public error codes returned by commands and APIs."""

from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_INPUT = "INVALID_INPUT"
    STALE_VERSION = "STALE_VERSION"
    LEASE_CONFLICT = "LEASE_CONFLICT"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    CAPACITY_EXHAUSTED = "CAPACITY_EXHAUSTED"
    POLICY_VIOLATION = "POLICY_VIOLATION"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    EXTERNAL_TRANSIENT = "EXTERNAL_TRANSIENT"


class DomainError(Exception):
    """A normalized error. Messages must be safe public descriptions, not raw output.

    Retryable describes eligibility, not permission for an immediate retry.
    Capacity requires scheduling; capability and verification need remediation.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code

    @property
    def retryable(self) -> bool:
        return self.code in {
            ErrorCode.STALE_VERSION,
            ErrorCode.LEASE_CONFLICT,
            ErrorCode.CAPACITY_EXHAUSTED,
            ErrorCode.EXTERNAL_TRANSIENT,
        }
