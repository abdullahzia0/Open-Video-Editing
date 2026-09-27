"""Thin MCP transport layer. Business logic lives in application services."""

import json
from collections.abc import Callable
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import ValidationError

from ove.application.service import EditingService
from ove.captions.subtitles import serialize
from ove.domain.errors import OveError
from ove.domain.models import CaptionCue, PlanRequest
from ove.utilities.identity import new_id


def result(call: Callable[[], Any]) -> CallToolResult:
    try:
        data = call()
        envelope = {"schema_version": "1", "request_id": new_id("request"), "data": data}
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(envelope))],
            structuredContent=envelope,
        )
    except (OveError, ValidationError, ValueError) as exc:
        error = (
            exc.as_dict()
            if isinstance(exc, OveError)
            else {
                "code": "invalid_input",
                "message": str(exc),
                "retryable": False,
            }
        )
        envelope = {"schema_version": "1", "request_id": new_id("request"), "error": error}
        return CallToolResult(
            isError=True,
            content=[TextContent(type="text", text=json.dumps(envelope))],
            structuredContent=envelope,
        )


def create_server(service: EditingService) -> FastMCP:
    server = FastMCP(
        "Open Video Editing",
        host="127.0.0.1",
        port=8765,
        instructions="Use system_capabilities before planning. "
        "Never report queued work as complete. "
        "Honor allowed_effects; text times refer to the final timeline. Run a separate ove worker.",
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    @server.tool(annotations=read)
    def system_capabilities() -> CallToolResult:
        """Inspect installed engines, supported operations and unavailable providers."""
        return result(service.capabilities)

    @server.tool(annotations=write)
    def assets_import(source: str) -> CallToolResult:
        """Copy and inspect a local file under configured roots. URLs are not supported."""
        return result(lambda: service.import_asset(source))

    @server.tool(annotations=read)
    def assets_inspect(asset_id: str) -> CallToolResult:
        """Read registered media metadata without changing the source."""
        return result(lambda: service.repository.get("asset", asset_id))

    @server.tool(annotations=write)
    def projects_create(name: str, asset_ids: list[str]) -> CallToolResult:
        """Create a project referencing previously imported assets."""
        return result(lambda: service.create_project(name, asset_ids))

    @server.tool(annotations=read)
    def projects_get(project_id: str, revision: int | None = None) -> CallToolResult:
        """Read the current project or an immutable historical revision."""
        return result(
            lambda: service.repository.get(
                "project" if revision is None else "revision",
                project_id if revision is None else f"{project_id}:{revision}",
            )
        )

    @server.tool(annotations=write)
    def projects_revise(
        project_id: str, expected_revision: int, asset_ids: list[str]
    ) -> CallToolResult:
        """Create a revision of project asset membership with optimistic concurrency."""
        return result(lambda: service.revise_project(project_id, expected_revision, asset_ids))

    @server.tool(annotations=read)
    def formats_list() -> CallToolResult:
        """List installed data presets; platform labels are advisory, not upload guarantees."""
        return result(service.presets.catalog)

    @server.tool(annotations=read)
    def formats_resolve(
        preset_ids: list[str], overrides: dict[str, Any] | None = None
    ) -> CallToolResult:
        """Compose preset fragments; conflicting values need an explicit override."""
        return result(lambda: service.presets.resolve(preset_ids, overrides))

    @server.tool(annotations=write)
    def plans_create(request: PlanRequest) -> CallToolResult:
        """Validate typed edits and store an immutable plan/hash. Does not render."""
        return result(lambda: service.create_plan(request))

    @server.tool(annotations=read)
    def plans_validate(plan_id: str, plan_hash: str) -> CallToolResult:
        """Recheck exact plan hash, constraints and engine availability before execution."""
        return result(lambda: service.validate_plan(plan_id, plan_hash))

    @server.tool(annotations=write)
    def render_submit(plan_id: str, plan_hash: str, idempotency_key: str) -> CallToolResult:
        """Queue an exact validated plan. Returns a job, not a completed video."""
        return result(lambda: service.submit_render(plan_id, plan_hash, idempotency_key))

    @server.tool(annotations=read)
    def jobs_get(job_id: str) -> CallToolResult:
        """Read job state and receipts. Only succeeded indicates a validated completed output."""
        return result(lambda: service.repository.job(job_id))

    @server.tool(annotations=write)
    def jobs_cancel(job_id: str) -> CallToolResult:
        """Request cancellation; running jobs stop when the worker observes the request."""
        return result(lambda: service.repository.cancel(job_id))

    @server.tool(annotations=write)
    def jobs_retry(job_id: str, idempotency_key: str) -> CallToolResult:
        """Retry failed/cancelled work using a new key; revalidates rendering plans."""
        return result(lambda: service.retry(job_id, idempotency_key))

    @server.tool(annotations=read)
    def artifacts_get(artifact_id: str) -> CallToolResult:
        """Return a successful local artifact path, hash, size and validation evidence."""
        return result(lambda: service.artifact(artifact_id))

    @server.tool(annotations=write)
    def transcripts_create(
        asset_id: str, idempotency_key: str, language: str | None = None
    ) -> CallToolResult:
        """Queue optional local faster-whisper ASR; requires installed model files and package."""
        return result(lambda: service.submit_transcript(asset_id, language, idempotency_key))

    @server.tool(annotations=read)
    def transcripts_get(transcript_id: str) -> CallToolResult:
        """Read a completed transcript with estimated timing provenance."""

        def get() -> dict[str, Any]:
            transcript = service.repository.get("transcript", transcript_id)
            if service.repository.job(transcript["job_id"])["state"] != "succeeded":
                raise OveError("transcript_not_ready", "The ASR job has not succeeded.")
            return transcript

        return result(get)

    @server.tool(annotations=read)
    def subtitles_export(
        cues: list[CaptionCue], format_name: Literal["srt", "vtt"]
    ) -> CallToolResult:
        """Serialize supplied caption text/times to SRT or WebVTT; does not invent transcription."""
        return result(lambda: {"format": format_name, "text": serialize(cues, format_name)})

    @server.tool(annotations=read)
    def connections_status() -> CallToolResult:
        """Report design-provider readiness; no credentials or tokens are returned."""
        return result(service.design.capabilities)

    @server.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            openWorldHint=True,
        )
    )
    def designs_execute(action: str, arguments: dict[str, Any]) -> CallToolResult:
        """Design-provider boundary. Canva returns an actionable unavailable error."""
        return result(lambda: service.design.execute(action, arguments))

    @server.resource("ove://capabilities")
    def capabilities_resource() -> str:
        return json.dumps(service.capabilities())

    @server.resource("ove://schemas/plan/1")
    def plan_schema_resource() -> str:
        return json.dumps(PlanRequest.model_json_schema())

    return server
