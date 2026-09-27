"""Uniform MCP result envelopes with typed, per-tool output schemas.

Every tool declares its success payload as a Pydantic model and returns
``Envelope[Payload]``. FastMCP then publishes a real JSON output schema for the
tool, while errors still travel as structured ``CallToolResult(isError=True)``
bodies rather than free-form prose.

A tool body never returns raw dicts to the client; it calls :func:`respond`,
which converts any failure into one of the canonical codes in
:mod:`ove.domain.errors`.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ove.domain.errors import OveError
from ove.utilities.identity import new_id


class ErrorBody(BaseModel):
    """Machine-readable failure. ``code`` is always a canonical taxonomy value."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Canonical error code, for example FILE_NOT_FOUND.")
    message: str = Field(description="What failed, in one sentence.")
    action: str | None = Field(default=None, description="The corrective step to take.")
    retryable: bool = Field(default=False, description="Whether retrying unchanged may succeed.")
    stage: str | None = Field(default=None, description="Pipeline stage that failed.")
    details: dict[str, Any] | None = Field(default=None, description="Extra structured evidence.")


class Envelope[T](BaseModel):
    """Stable response shape. Exactly one of ``data`` or ``error`` is populated."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    request_id: str
    status: Literal["ok", "error"]
    data: T | None = None
    error: ErrorBody | None = None
    warnings: list[str] = Field(default_factory=list)


@dataclass
class Outcome:
    """A handler result that carries advisory warnings alongside its payload."""

    data: Any
    warnings: list[str] = field(default_factory=list)


def _build(body: dict[str, Any], *, is_error: bool) -> CallToolResult:
    return CallToolResult(
        isError=is_error,
        content=[TextContent(type="text", text=json.dumps(body, ensure_ascii=False))],
        structuredContent=body,
    )


def ok(data: Any, warnings: list[str] | None = None) -> CallToolResult:
    body: dict[str, Any] = {
        "schema_version": "1",
        "request_id": new_id("request"),
        "status": "ok",
        "data": data,
        "warnings": list(warnings or []),
    }
    return _build(body, is_error=False)


def failure(
    code: str,
    message: str,
    *,
    action: str | None = None,
    retryable: bool = False,
    stage: str | None = None,
    details: dict[str, Any] | None = None,
) -> CallToolResult:
    body: dict[str, Any] = {
        "schema_version": "1",
        "request_id": new_id("request"),
        "status": "error",
        "error": {
            "code": code,
            "message": message,
            "action": action,
            "retryable": retryable,
            "stage": stage,
            "details": details,
        },
    }
    return _build(body, is_error=True)


def _from_exception(exc: BaseException) -> CallToolResult:
    if isinstance(exc, OveError):
        body = exc.as_dict()
        return failure(
            body["code"],
            body["message"],
            action=body.get("action"),
            retryable=body["retryable"],
            stage=body.get("stage"),
            details=body.get("details"),
        )
    if isinstance(exc, ValidationError):
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ())) or "arguments"
        return failure(
            "INVALID_INPUT",
            f"{location}: {first.get('msg', 'invalid value')}",
            action="Correct the reported field and call the tool again.",
            details={"errors": json.loads(exc.json())} if len(exc.errors()) > 1 else None,
        )
    if isinstance(exc, FileNotFoundError):
        return failure("FILE_NOT_FOUND", str(exc) or "The requested file does not exist.")
    if isinstance(exc, PermissionError):
        return failure("PERMISSION_DENIED", str(exc) or "Access to the resource was denied.")
    if isinstance(exc, ToolError):
        return failure(
            "INVALID_INPUT",
            str(exc),
            action="Correct the reported arguments and call the tool again.",
        )
    if isinstance(exc, ValueError | TypeError | KeyError):
        return failure("INVALID_INPUT", f"{type(exc).__name__}: {exc}")
    return failure(
        "PROCESSING_FAILED",
        "The operation failed unexpectedly. No partial result was published.",
        action="Review server logs; retry with a new idempotency key.",
        retryable=True,
    )


def respond(call: Callable[[], Any]) -> CallToolResult:
    """Run a tool body and convert its result or failure into an envelope."""
    try:
        result = call()
    except Exception as exc:  # noqa: BLE001 - every failure becomes a typed envelope
        return _from_exception(exc)
    if isinstance(result, CallToolResult):
        return result
    if isinstance(result, Outcome):
        return ok(result.data, result.warnings)
    return ok(result)


class StructuredFastMCP(FastMCP):
    """FastMCP that reports argument-validation failures as structured errors.

    FastMCP validates tool arguments before the tool body runs, so those failures
    never reach :func:`respond`. This override converts them into the same
    canonical envelope so a client sees one error shape for every failure mode.
    """

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            return _from_exception(exc)
        except Exception as exc:  # noqa: BLE001 - the client still gets an envelope
            return _from_exception(exc)
