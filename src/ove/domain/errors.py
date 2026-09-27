"""Canonical, actionable domain failures. Transport independent.

Every failure surfaces one of the machine-readable codes below so an assistant
can branch on it without parsing prose. The codes are stable API.

``INVALID_INPUT``          arguments or plan semantics are wrong; fix the request.
``FILE_NOT_FOUND``         a referenced asset, project, job or artifact is absent.
``UNSUPPORTED_FORMAT``     the installed engine cannot perform this on this input.
``PROCESSING_FAILED``      the engine ran and failed, or output validation failed.
``AUTH_REQUIRED``          an external provider needs an interactive authorization.
``PERMISSION_DENIED``      a path or resource is outside the configured policy.
``EXTERNAL_SERVICE_ERROR`` a remote provider was reachable but did not succeed.
``EXPORT_FAILED``          delivery of a finished render to its destination failed.
``CANCELLED``              the caller cancelled, or a worker observed cancellation.
``CONFLICT``               optimistic-concurrency or idempotency conflict.

``CONFLICT`` is an explicit extension to the requested taxonomy: folding a
concurrency conflict into ``INVALID_INPUT`` would tell an assistant to "fix the
arguments" when the correct recovery is "re-read state and retry". Every other
internal code in this codebase is an alias of the ten codes above.
"""

from __future__ import annotations

from typing import Any, Literal, get_args

ErrorCode = Literal[
    "INVALID_INPUT",
    "FILE_NOT_FOUND",
    "UNSUPPORTED_FORMAT",
    "PROCESSING_FAILED",
    "AUTH_REQUIRED",
    "PERMISSION_DENIED",
    "EXTERNAL_SERVICE_ERROR",
    "EXPORT_FAILED",
    "CANCELLED",
    "CONFLICT",
]

CANONICAL_CODES: frozenset[str] = frozenset(get_args(ErrorCode))

#: Internal codes raised across the engine and application layers. They are never
#: exposed to a client; :class:`OveError` normalizes them at construction time.
_ALIASES: dict[str, str] = {
    # Absent or unusable records.
    "not_found": "FILE_NOT_FOUND",
    "invalid_blob": "FILE_NOT_FOUND",
    "artifact_not_ready": "FILE_NOT_FOUND",
    "transcript_not_ready": "FILE_NOT_FOUND",
    "job_not_found": "FILE_NOT_FOUND",
    # Engine or input limitations.
    "invalid_media": "UNSUPPORTED_FORMAT",
    "unsupported_media": "UNSUPPORTED_FORMAT",
    "incompatible_audio": "UNSUPPORTED_FORMAT",
    "unsupported_operation": "UNSUPPORTED_FORMAT",
    "unknown_preset": "UNSUPPORTED_FORMAT",
    "missing_presets": "UNSUPPORTED_FORMAT",
    "invalid_preset": "UNSUPPORTED_FORMAT",
    "unsupported_text": "UNSUPPORTED_FORMAT",
    "no_audio": "UNSUPPORTED_FORMAT",
    "no_video": "UNSUPPORTED_FORMAT",
    "missing_dependency": "UNSUPPORTED_FORMAT",
    # Failures while doing work.
    "engine_failed": "PROCESSING_FAILED",
    "validation_failed": "PROCESSING_FAILED",
    "empty_output": "PROCESSING_FAILED",
    "worker_interrupted": "PROCESSING_FAILED",
    "timeout": "PROCESSING_FAILED",
    "internal_error": "PROCESSING_FAILED",
    "engine_changed": "PROCESSING_FAILED",
    "source_changed": "PROCESSING_FAILED",
    "invalid_transition": "PROCESSING_FAILED",
    "unsupported_job": "PROCESSING_FAILED",
    "invalid_capabilities": "PROCESSING_FAILED",
    "schema_mismatch": "PROCESSING_FAILED",
    "transcript_failed": "PROCESSING_FAILED",
    # Provider boundary.
    "provider_unavailable": "EXTERNAL_SERVICE_ERROR",
    "provider_error": "EXTERNAL_SERVICE_ERROR",
    "not_connected": "AUTH_REQUIRED",
    "authorization_pending": "AUTH_REQUIRED",
    # Policy boundary.
    "invalid_source": "PERMISSION_DENIED",
    "path_denied": "PERMISSION_DENIED",
    "policy_denied": "PERMISSION_DENIED",
    # Caller mistakes.
    "resource_limit": "INVALID_INPUT",
    "invalid_range": "INVALID_INPUT",
    "invalid_crop": "INVALID_INPUT",
    "invalid_dimensions": "INVALID_INPUT",
    "invalid_project": "INVALID_INPUT",
    "invalid_asset": "INVALID_INPUT",
    "invalid_idempotency_key": "INVALID_INPUT",
    "invalid_request": "INVALID_INPUT",
    "scope_violation": "INVALID_INPUT",
    "operation_order": "INVALID_INPUT",
    "not_retryable": "INVALID_INPUT",
    "unsupported_translation": "INVALID_INPUT",
    # Concurrency.
    "plan_mismatch": "CONFLICT",
    "idempotency_conflict": "CONFLICT",
    "revision_conflict": "CONFLICT",
    "conflict": "CONFLICT",
    "worker_busy": "CONFLICT",
    # Cancellation.
    "cancelled": "CANCELLED",
    "cancel_requested": "CANCELLED",
}


def canonical_code(code: str) -> str:
    """Map any internal or legacy code onto the public taxonomy."""
    if code in CANONICAL_CODES:
        return code
    return _ALIASES.get(code, "PROCESSING_FAILED")


class OveError(Exception):
    """A failure with a machine-readable code and a corrective action.

    ``code`` is always one of :data:`CANONICAL_CODES`. ``detail_code`` preserves
    the original internal code for diagnostics and worker state transitions.
    """

    def __init__(
        self,
        code: str,
        message: str,
        action: str = "",
        retryable: bool = False,
        *,
        stage: str | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.detail_code = code
        self.code = canonical_code(code)
        self.action = action
        self.retryable = retryable
        self.stage = stage
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "code": self.code,
            "message": str(self),
            "retryable": self.retryable,
        }
        if self.action:
            body["action"] = self.action
        if self.stage:
            body["stage"] = self.stage
        if self.details:
            body["details"] = self.details
        return body
