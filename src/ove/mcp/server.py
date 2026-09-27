"""MCP transport layer. Business logic lives in application services.

Design rules enforced by this module:

* **Intent, not parameters.** Each tool performs one editorial action a person
  would ask for ("trim this", "add a lower third"). Raw FFmpeg flags are never
  accepted; the engine compiles filters only from typed operations.
* **One response shape.** Every tool returns ``Envelope[Payload]``, so clients get
  a published JSON output schema and exactly one error shape.
* **Structured failures only.** Failures carry a canonical code from
  :mod:`ove.domain.errors` plus a corrective action. No tool returns prose.
* **Queued work is never reported as finished.** Rendering tools return a job
  handle; only ``state == "succeeded"`` means a validated file exists.
* **No secrets cross the boundary.** Tokens, client secrets and key material stay
  in the server's encrypted credential store.

Output payloads are built by the application layer and validated here against the
declared model, so a drift between the two surfaces as an error rather than a
silent contract change.
"""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal, cast

from mcp.types import CallToolResult, ToolAnnotations

from ove.application.planning import EFFECTS, JOB_EFFECTS, export_effects
from ove.application.service import EditingService
from ove.captions.subtitles import serialize
from ove.captions.text import suggestions
from ove.domain.errors import OveError
from ove.domain.models import (
    AudioExtractSpec,
    AudioFade,
    AudioMixSpec,
    AudioNormalize,
    AudioProcessSpec,
    AudioReplaceSpec,
    Captions,
    Color,
    ColorGrade,
    ConcatSpec,
    Crop,
    Cut,
    Denoise,
    Effect,
    ExportSpec,
    Flip,
    FrameSpec,
    Freeze,
    Intent,
    Lighting,
    MediaJobRequest,
    OverlaySpec,
    PlanRequest,
    ProgressBar,
    Resize,
    Rotate,
    Sharpen,
    Speed,
    SplitSpec,
    Stabilize,
    TextOverlay,
    TimeRange,
    Trim,
    Upscale,
    Zoom,
)
from ove.mcp.envelope import Envelope, StructuredFastMCP, respond
from ove.mcp.schemas import (
    AssetListOut,
    AssetOut,
    CanvaConnectOut,
    CanvaDesignListOut,
    CanvaDesignOut,
    CanvaDesignTypeInput,
    CanvaExportOut,
    CanvaUploadOut,
    CapabilitiesOut,
    CaptionCuesOut,
    ExportOut,
    FormatListOut,
    FormatOut,
    FormatValidationOut,
    JobHandle,
    JobListOut,
    JobStatus,
    MediaInfoOut,
    ProjectListOut,
    ProjectOut,
    TextSuggestionsOut,
    TranscriptionJob,
    TranslationOut,
)

# ---------------------------------------------------------------------------
# Shared vocabulary
# ---------------------------------------------------------------------------

Quality = Literal["draft", "standard", "high", "master"]

#: Encoder speed presets, mirrored from :class:`ove.domain.models.ExportSpec`.
EncoderPreset = Literal["ultrafast", "fast", "medium", "slow"]

#: Delivery quality -> (CRF, encoder preset). Callers choose intent, not numbers.
QUALITY: dict[str, tuple[int, EncoderPreset]] = {
    "draft": (28, "ultrafast"),
    "standard": (23, "fast"),
    "high": (18, "medium"),
    "master": (14, "slow"),
}

CONTAINERS = Literal["mp4", "mkv", "webm", "mov"]
VIDEO_CODECS = Literal["h264", "hevc", "vp9", "copy"]
AUDIO_MODES = Literal["copy", "aac", "opus", "drop"]
FITS = Literal["pad", "crop", "stretch"]
POSITIONS = Literal[
    "center", "top", "bottom", "top_left", "top_right", "bottom_left", "bottom_right"
]
ANIMATIONS = Literal["none", "fade", "slide_up", "slide_down", "slide_left", "pop", "typewriter"]
OVERLAY_ANIMATIONS = Literal["none", "fade", "slide_in"]
TEMPLATES = Literal["title", "lower_third", "callout", "social_handle", "cta", "hook", "caption"]
CAPTION_STYLE = Literal["modern", "lower-third", "bold", "minimal", "highlight"]
LOOKS = Literal["neutral", "warm", "cool", "cinematic", "vintage", "noir", "vivid", "faded"]
TRANSITIONS = Literal[
    "none",
    "fade",
    "dissolve",
    "wipeleft",
    "wiperight",
    "slideup",
    "slidedown",
    "circleopen",
    "circleclose",
    "pixelize",
    "radial",
]

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
REMOTE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)

WAIT_HELP = (
    "0 queues the job and returns immediately; a positive value lets this call drive the "
    "local worker until the job reaches a terminal state or the budget expires."
)


# ---------------------------------------------------------------------------
# Request builders
# ---------------------------------------------------------------------------


def _export(
    quality: Quality = "high",
    *,
    width: int | None = None,
    height: int | None = None,
    fit: FITS = "pad",
    container: CONTAINERS = "mp4",
    video_codec: VIDEO_CODECS = "h264",
    audio: AUDIO_MODES = "aac",
    audio_bitrate_kbps: int = 192,
) -> ExportSpec:
    crf, preset = QUALITY[quality]
    return ExportSpec(
        container=container,
        video_codec=video_codec,
        crf=crf,
        encoder_preset=cast(EncoderPreset, preset),
        audio=audio,
        audio_bitrate_kbps=audio_bitrate_kbps,
        width=width,
        height=height,
        fit=fit,
    )


def _render(
    service: EditingService,
    asset_id: str,
    operations: list[Any],
    *,
    export: ExportSpec,
    note: str,
    project_id: str | None = None,
    revision: int | None = None,
    idempotency_key: str | None = None,
    wait_seconds: float = 0.0,
) -> dict[str, Any]:
    """Plan, freeze and queue one single-source edit."""
    effects: set[Effect] = {EFFECTS[operation.type] for operation in operations}
    effects |= export_effects(export)
    request = PlanRequest(
        project_id=project_id,
        revision=revision,
        asset_id=asset_id,
        operations=operations,
        intent=Intent(request=note, allowed_effects=effects),
        export=export,
    )
    return service.plan_and_submit(request, idempotency_key, wait_seconds)


def _job(
    service: EditingService,
    asset_ids: list[str],
    spec: Any,
    *,
    export: ExportSpec | None = None,
    note: str,
    project_id: str | None = None,
    revision: int | None = None,
    idempotency_key: str | None = None,
    wait_seconds: float = 0.0,
) -> dict[str, Any]:
    """Plan, freeze and queue one multi-input or non-video job."""
    job_export = export if export is not None else ExportSpec(audio="aac")
    allowed: set[Effect] = set(JOB_EFFECTS[spec.type])
    allowed |= export_effects(job_export)
    request = MediaJobRequest(
        project_id=project_id,
        revision=revision,
        asset_ids=list(asset_ids),
        job=spec,
        export=job_export,
        intent=Intent(request=note, allowed_effects=allowed),
    )
    return service.plan_and_submit(request, idempotency_key, wait_seconds)


def _project(record: dict[str, Any]) -> dict[str, Any]:
    """Map a stored project record onto the published project contract."""
    return {
        "project_id": record["id"],
        "name": record["name"],
        "revision": record["revision"],
        "asset_ids": list(record["asset_ids"]),
        "created_at": record["created_at"],
    }


def _cues(cues: list[Any], **extra: Any) -> dict[str, Any]:
    return {
        "cues": [cue.model_dump(mode="json") for cue in cues],
        "count": len(cues),
        **extra,
    }


def _sidecar(cues: list[Any], sidecar_format: str | None) -> dict[str, Any]:
    if sidecar_format is None:
        return {"format": None, "text": None}
    return {"format": sidecar_format, "text": serialize(cues, sidecar_format)}


def _suggest(
    kind: str, subject: str, detail: str | None, audience: str | None, limit: int
) -> dict[str, Any]:
    """Compose template-based copy and label its provenance honestly."""
    return {
        "kind": kind,
        "suggestions": suggestions(kind, subject, detail, audience, limit),  # type: ignore[arg-type]
        "source": "template",
        "model_generated": False,
        "notes": [
            "Composed from built-in templates using the subject you supplied.",
            "No language model was invoked; review the wording before publishing.",
        ],
    }


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def create_server(service: EditingService) -> StructuredFastMCP:
    server = StructuredFastMCP(
        "Open Video Editing",
        host="127.0.0.1",
        port=8765,
        instructions=(
            "Local, non-destructive video editing. Call system_capabilities first to see what "
            "this install can do. Rendering tools queue durable jobs: a job is finished only "
            "when get_job_status reports state 'succeeded' with artifacts. A separate local "
            "worker must run for queued work to progress. Source files are never modified. "
            "Canva operations require the canva_connect authorization flow and are unavailable "
            "when the integration is not configured."
        ),
    )

    # ---------------------------------------------------------------- system

    @server.tool(title="System capabilities", annotations=READ)
    def system_capabilities() -> Annotated[CallToolResult, Envelope[CapabilitiesOut]]:
        """Report installed engines, supported operations, job kinds and provider readiness.

        Call this before planning work. Unavailable capabilities are listed explicitly
        rather than failing later during rendering.
        """
        return respond(service.capabilities)

    @server.tool(title="Import a media file", annotations=WRITE)
    def import_asset(source: str) -> Annotated[CallToolResult, Envelope[AssetOut]]:
        """Copy a local file into immutable content-addressed storage and probe it.

        The source must live under a configured local root; remote URLs are not
        fetched. The original file is read only. Re-importing identical bytes is
        safe: the stored blob is keyed by its SHA-256 digest.
        """
        return respond(lambda: service.asset_info(service.import_asset(source)["id"]))

    @server.tool(title="List jobs", annotations=READ)
    def list_jobs(
        state: str | None = None, limit: int = 20, offset: int = 0
    ) -> Annotated[CallToolResult, Envelope[JobListOut]]:
        """List queued and historical jobs, newest first, optionally filtered by state."""
        return respond(lambda: service.list_jobs(state, limit, offset))

    @server.tool(title="Retry a job", annotations=WRITE)
    def retry_job(
        job_id: str, idempotency_key: str
    ) -> Annotated[CallToolResult, Envelope[JobStatus]]:
        """Re-queue a failed or cancelled job. A new idempotency key is required.

        The plan is revalidated against the current source checksums and engine
        before the new attempt is queued.
        """
        return respond(lambda: service.retry(job_id, idempotency_key))

    # ---------------------------------------------------------------- video

    @server.tool(title="Inspect media", annotations=READ)
    def video_info(
        source: str, as_path: bool = False
    ) -> Annotated[CallToolResult, Envelope[MediaInfoOut]]:
        """Probe an imported asset (default) or an allowlisted local path for duration,
        dimensions, codecs and stream layout.

        Set as_path=true to inspect a file that has not been imported. Nothing is
        modified and no asset record is created.
        """
        return respond(lambda: service.media_info(source, as_path))

    @server.tool(title="Trim a video", annotations=WRITE)
    def video_trim(
        asset_id: str,
        start: float,
        end: float,
        quality: Quality = "high",
        project_id: str | None = None,
        revision: int | None = None,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Keep only the section between start and end seconds, discarding the rest.

        Queued, not rendered. `end` must be greater than `start` and inside the
        source. Identical arguments reuse the same job unless a new
        idempotency_key is supplied.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Trim(start=start, end=end)],
                export=_export(quality),
                note=f"Trim {asset_id} to {start}-{end}s",
                project_id=project_id,
                revision=revision,
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Cut sections out", annotations=WRITE)
    def video_cut(
        asset_id: str,
        ranges: list[TimeRange],
        quality: Quality = "high",
        project_id: str | None = None,
        revision: int | None = None,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Remove the listed intervals and join what remains, closing the gap.

        Ranges are half-open `[start, end)`, must not overlap, and are applied to
        source time. The result is shorter than the source by the total removed
        duration. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Cut(ranges=ranges)],
                export=_export(quality),
                note=f"Cut {len(ranges)} interval(s) from {asset_id}",
                project_id=project_id,
                revision=revision,
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Split into segments", annotations=WRITE)
    def video_split(
        asset_id: str,
        boundaries: list[float],
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Export consecutive segments of one video as separate artifacts.

        `boundaries` are strictly increasing cut points in seconds; N boundaries
        produce N+1 files, each validated independently. Queued, not rendered.
        """
        return respond(
            lambda: _job(
                service,
                [asset_id],
                SplitSpec(boundaries=boundaries),
                export=_export(quality),
                note=f"Split {asset_id} at {len(boundaries)} boundary(ies)",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Merge videos", annotations=WRITE)
    def video_merge(
        asset_ids: list[str],
        transition: TRANSITIONS = "none",
        transition_duration: float = 0.5,
        normalize: bool = True,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Join two or more videos in the order given, optionally cross-fading.

        With normalize=true each clip is scaled and padded to the first clip's
        canvas so mixed resolutions merge cleanly. A transition shortens the total
        duration by `transition_duration` per join. Queued, not rendered.
        """
        return respond(
            lambda: _job(
                service,
                asset_ids,
                ConcatSpec(
                    transition=transition,
                    transition_duration=transition_duration,
                    normalize=normalize,
                ),
                export=_export(quality),
                note=f"Merge {len(asset_ids)} clips",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Crop a video", annotations=WRITE)
    def video_crop(
        asset_id: str,
        x: int,
        y: int,
        width: int,
        height: int,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Cut the frame down to a rectangle at (x, y) of the given size.

        Coordinates are even pixels measured from the top-left of the current
        canvas. The rectangle must fit inside the frame. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Crop(x=x, y=y, width=width, height=height)],
                export=_export(quality),
                note=f"Crop {asset_id} to {width}x{height} at ({x},{y})",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Resize a video", annotations=WRITE)
    def video_resize(
        asset_id: str,
        width: int,
        height: int,
        fit: FITS = "pad",
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Scale the frame to an exact canvas using a fit policy.

        `pad` letterboxes without distorting, `crop` fills and trims the overflow,
        `stretch` distorts to fit. Dimensions must be even and at most 7680.
        Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Resize(width=width, height=height, fit=fit)],
                export=_export(quality),
                note=f"Resize {asset_id} to {width}x{height} ({fit})",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Rotate a video", annotations=WRITE)
    def video_rotate(
        asset_id: str,
        degrees: Literal[90, 180, 270],
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Rotate the frame clockwise by 90, 180 or 270 degrees.

        A 90 or 270 degree rotation swaps width and height. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Rotate(degrees=degrees)],
                export=_export(quality),
                note=f"Rotate {asset_id} by {degrees} degrees",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Flip a video", annotations=WRITE)
    def video_flip(
        asset_id: str,
        axis: Literal["horizontal", "vertical"],
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Mirror the frame horizontally (left-right) or vertically (top-bottom).

        Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Flip(axis=axis)],
                export=_export(quality),
                note=f"Flip {asset_id} on the {axis} axis",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Change playback speed", annotations=WRITE)
    def video_speed(
        asset_id: str,
        factor: float,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Speed the whole clip up (factor > 1) or slow it down (factor < 1).

        Accepts 0.25 to 4.0. Audio is pitch-preserved via tempo resampling; video
        is resampled to a constant frame rate. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Speed(factor=factor)],
                export=_export(quality),
                note=f"Change {asset_id} speed by {factor}x",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Freeze a frame", annotations=WRITE)
    def video_freeze_frame(
        asset_id: str,
        at: float,
        duration: float,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Hold the frame at `at` for `duration` seconds and resume playback.

        Audio is silent across the held frame; the original audio continues
        afterwards. `duration` is capped at 120 seconds. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Freeze(at=at, duration=duration)],
                export=_export(quality),
                note=f"Freeze {asset_id} at {at}s for {duration}s",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Extract a still frame", annotations=WRITE)
    def video_extract_frame(
        asset_id: str,
        at: float,
        image_format: Literal["jpg", "png", "webp"] = "jpg",
        jpeg_quality: int = 2,
        width: int | None = None,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Export one still image from the video at the given second.

        `jpeg_quality` is the encoder quantiser: 1 is best and 31 is worst; it is
        ignored for PNG. `width` scales the still and keeps the aspect ratio.
        Produces an image artifact. Queued, not rendered.
        """
        return respond(
            lambda: _job(
                service,
                [asset_id],
                FrameSpec(at=at, format=image_format, quality=jpeg_quality, width=width),
                note=f"Extract a frame from {asset_id} at {at}s",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Stabilize a video", annotations=WRITE)
    def video_stabilize(
        asset_id: str,
        smoothing: int = 15,
        crop: Literal["black", "keep"] = "black",
        zoom: float = 0.0,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Reduce handheld shake using two-pass motion analysis.

        Higher `smoothing` (1-60) yields a calmer but more reframed result.
        `crop=black` fills exposed edges with black; `keep` leaves them. `zoom`
        (0-10) trades field of view for stability. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Stabilize(smoothing=smoothing, crop=crop, zoom=zoom)],
                export=_export(quality),
                note=f"Stabilize {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Reduce noise", annotations=WRITE)
    def video_denoise(
        asset_id: str,
        strength: float = 2.0,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Suppress low-light grain and compression noise temporally and spatially.

        `strength` runs 0.1 to 6.0; higher values remove more noise and more fine
        detail. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Denoise(strength=strength)],
                export=_export(quality),
                note=f"Denoise {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Sharpen a video", annotations=WRITE)
    def video_sharpen(
        asset_id: str,
        amount: float = 0.5,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Increase local contrast on edges to recover perceived detail.

        `amount` runs 0.1 to 1.5. Sharpening also amplifies noise, so denoise
        first when the source is grainy. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Sharpen(amount=amount)],
                export=_export(quality),
                note=f"Sharpen {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Adjust brightness and colour", annotations=WRITE)
    def video_color_adjust(
        asset_id: str,
        brightness: float = 0.0,
        contrast: float = 1.0,
        gamma: float = 1.0,
        saturation: float = 1.0,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Apply correction controls to the whole clip.

        brightness -0.3..0.3, contrast 0.5..1.5, gamma 0.5..2.0 and saturation
        0..2. Neutral values are ignored. Queued, not rendered.
        """
        operations: list[Any] = []
        if brightness or contrast != 1.0 or gamma != 1.0:
            operations.append(Lighting(brightness=brightness, contrast=contrast, gamma=gamma))
        if saturation != 1.0:
            operations.append(Color(saturation=saturation))
        if not operations:
            raise OveError(
                "invalid_input",
                "No colour control differs from its neutral value.",
                "Change at least one of brightness, contrast, gamma or saturation.",
            )
        return respond(
            lambda: _render(
                service,
                asset_id,
                operations,
                export=_export(quality),
                note=f"Adjust colour of {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Apply a creative look", annotations=WRITE)
    def video_color_grade(
        asset_id: str,
        look: LOOKS = "neutral",
        intensity: float = 1.0,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Apply a named creative grade such as warm, cinematic or noir.

        `intensity` blends the look toward the untouched source: 0 is neutral and
        1 is the full look. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [ColorGrade(look=look, intensity=intensity)],
                export=_export(quality),
                note=f"Grade {asset_id} with the {look} look",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Upscale resolution", annotations=WRITE)
    def video_upscale(
        asset_id: str,
        width: int,
        height: int,
        fit: FITS = "pad",
        sharpen: float = 0.0,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Resample the frame up to a larger canvas.

        This is conventional resampling with an optional sharpen pass; it does not
        invent detail that was never captured. Neural super-resolution is not
        offered. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Upscale(width=width, height=height, fit=fit, sharpen=sharpen)],
                export=_export(quality),
                note=f"Upscale {asset_id} to {width}x{height}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Transcode a video", annotations=WRITE)
    def video_transcode(
        asset_id: str,
        container: CONTAINERS = "mp4",
        video_codec: VIDEO_CODECS = "h264",
        quality: Quality = "high",
        audio: AUDIO_MODES = "aac",
        audio_bitrate_kbps: int = 192,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Re-encode or remux a video into a different delivery format.

        Choose `video_codec="copy"` for a lossless remux, which requires
        `audio="copy"` or `"drop"` and is rejected when the container cannot hold
        the existing stream. Container and codec must be compatible, for example
        webm requires vp9. Queued, not rendered.
        """
        if video_codec == "copy" and audio not in {"copy", "drop"}:
            raise OveError(
                "invalid_input",
                "A stream-copy remux cannot re-encode audio in the same pass.",
                "Set audio to copy or drop, or pick h264, hevc or vp9 to re-encode.",
            )
        export = _export(
            quality,
            container=container,
            video_codec=video_codec,
            audio=audio,
            audio_bitrate_kbps=audio_bitrate_kbps,
        )
        return respond(
            lambda: _render(
                service,
                asset_id,
                [],
                export=export,
                note=f"Transcode {asset_id} to {container}/{video_codec}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    # ---------------------------------------------------------------- audio

    @server.tool(title="Extract audio", annotations=WRITE)
    def audio_extract(
        asset_id: str,
        container: Literal["m4a", "mp3", "wav", "opus", "webm"] = "m4a",
        bitrate_kbps: int = 192,
        sample_rate: int | None = None,
        channels: Literal[1, 2] | None = None,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Export the audio track of a video or audio asset as its own file.

        `wav` is uncompressed, so `bitrate_kbps` is ignored for it. `channels`
        downmixes to mono or stereo when supplied. Queued, not rendered.
        """
        return respond(
            lambda: _job(
                service,
                [asset_id],
                AudioExtractSpec(
                    container=container,
                    bitrate_kbps=bitrate_kbps,
                    sample_rate=sample_rate,
                    channels=channels,
                ),
                note=f"Extract {container} audio from {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Replace a video's audio", annotations=WRITE)
    def audio_replace(
        asset_id: str,
        audio_asset_id: str,
        mix_with_original: float = 0.0,
        loop: bool = True,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Swap a video's soundtrack for another asset's audio.

        `loop=true` repeats or trims the new audio to the video's length; false
        truncates the video to the shorter of the two. `mix_with_original` keeps
        0-1 of the original track under the replacement. Queued, not rendered.
        """
        return respond(
            lambda: _job(
                service,
                [asset_id, audio_asset_id],
                AudioReplaceSpec(
                    audio_asset_id=audio_asset_id,
                    mix_with_original=mix_with_original,
                    loop=loop,
                ),
                export=_export(quality),
                note=f"Replace the audio of {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Normalize loudness", annotations=WRITE)
    def audio_normalize(
        asset_id: str,
        target_lufs: float = -16.0,
        true_peak_db: float = -1.5,
        loudness_range: float = 11.0,
        container: Literal["m4a", "mp3", "wav", "opus", "webm", "keep"] = "keep",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Even out loudness to a broadcast-style target in one measuring pass.

        `target_lufs` is the integrated loudness (-40 to -5; -16 suits web, -14
        suits podcast), `true_peak_db` the ceiling, and `loudness_range` the
        permitted dynamic spread. Video assets keep their picture. Queued, not rendered.
        """
        return respond(
            lambda: _job(
                service,
                [asset_id],
                AudioProcessSpec(
                    container=container,
                    normalize=AudioNormalize(
                        target_lufs=target_lufs,
                        true_peak_db=true_peak_db,
                        loudness_range=loudness_range,
                    ),
                ),
                note=f"Normalize the audio of {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Fade audio in or out", annotations=WRITE)
    def audio_fade(
        asset_id: str,
        fade_in: float = 0.0,
        fade_out: float = 0.0,
        curve: Literal["tri", "qsin", "exp", "log", "par"] = "tri",
        container: Literal["m4a", "mp3", "wav", "opus", "webm", "keep"] = "keep",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Ramp the audio up from silence and/or down to silence.

        Durations are seconds (0-60); a value of 0 disables that fade. `curve`
        selects the ramp shape. Video assets keep their picture. Queued, not rendered.
        """
        if fade_in <= 0 and fade_out <= 0:
            raise OveError(
                "invalid_input",
                "Supply a fade_in, a fade_out, or both.",
                "A fade of 0 seconds changes nothing.",
            )
        return respond(
            lambda: _job(
                service,
                [asset_id],
                AudioProcessSpec(
                    container=container,
                    fade=AudioFade(fade_in=fade_in, fade_out=fade_out, curve=curve),
                ),
                note=f"Fade the audio of {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Trim audio", annotations=WRITE)
    def audio_trim(
        asset_id: str,
        start: float,
        end: float,
        container: Literal["m4a", "mp3", "wav", "opus", "webm", "keep"] = "keep",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Keep only the audio between start and end seconds.

        For a video asset the picture is retained and stays in sync; for an audio
        asset only the selected span is exported. Queued, not rendered.
        """
        return respond(
            lambda: _job(
                service,
                [asset_id],
                AudioProcessSpec(container=container, trim=TimeRange(start=start, end=end)),
                note=f"Trim the audio of {asset_id} to {start}-{end}s",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Mix audio tracks", annotations=WRITE)
    def audio_mix(
        asset_ids: list[str],
        weights: list[float] | None = None,
        duration: Literal["longest", "shortest", "first"] = "longest",
        video_asset_id: str | None = None,
        container: Literal["m4a", "mp3", "wav", "opus", "webm"] = "m4a",
        bitrate_kbps: int = 192,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Blend two or more audio sources together, for example a voiceover and music.

        `weights` sets one gain per asset (0-4); omit it for an equal mix.
        `duration` decides how differing lengths are reconciled. Pass
        `video_asset_id` to lay the mix over a video instead of exporting audio.
        Queued, not rendered.
        """
        if weights is not None and len(weights) != len(asset_ids):
            raise OveError(
                "invalid_input",
                "Supply one weight per mixed asset, or omit weights entirely.",
            )
        job_asset_ids = [*asset_ids, video_asset_id] if video_asset_id else list(asset_ids)
        return respond(
            lambda: _job(
                service,
                job_asset_ids,
                AudioMixSpec(
                    weights=weights,
                    duration=duration,
                    video_asset_id=video_asset_id,
                    container=container,
                    bitrate_kbps=bitrate_kbps,
                ),
                note=f"Mix {len(asset_ids)} audio source(s)",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Remove audio", annotations=WRITE)
    def audio_remove(
        asset_id: str,
        container: Literal["m4a", "mp3", "wav", "opus", "webm", "keep"] = "keep",
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Strip the audio track, producing a silent video or an empty audio asset.

        Cannot be combined with other audio operations. Queued, not rendered.
        """
        return respond(
            lambda: _job(
                service,
                [asset_id],
                AudioProcessSpec(container=container, remove=True),
                export=_export(quality),
                note=f"Remove the audio from {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    # -------------------------------------------------------------- captions

    @server.tool(title="Transcribe speech", annotations=WRITE)
    def transcribe_media(
        asset_id: str,
        language: str | None = None,
        idempotency_key: str | None = None,
    ) -> Annotated[CallToolResult, Envelope[TranscriptionJob]]:
        """Queue local speech-to-text for an asset that contains audio.

        Runs entirely on this machine using a locally installed model; nothing is
        uploaded. Leave `language` unset for automatic detection. Poll
        get_job_status, then pass the transcript_id to generate_captions.
        """
        return respond(lambda: service.transcribe_request(asset_id, language, idempotency_key))

    @server.tool(title="Generate captions", annotations=READ)
    def generate_captions(
        text: str | None = None,
        transcript_id: str | None = None,
        asset_id: str | None = None,
        duration: float | None = None,
        start: float = 0.0,
        max_characters: int = 84,
        max_duration: float = 6.0,
        sidecar_format: Literal["srt", "vtt"] | None = None,
    ) -> Annotated[CallToolResult, Envelope[CaptionCuesOut]]:
        """Turn a transcript or a block of text into readable, timed caption cues.

        Supply `transcript_id` for real speech timing, or `text` plus a duration.
        With `text` you may pass `asset_id` instead of `duration` to read the
        length from the asset. Cues are split to stay under `max_characters` and
        `max_duration`. Set `sidecar_format` to also return an SRT or WebVTT body.
        Nothing is rendered; pass the cues to burn_captions.
        """

        def build() -> dict[str, Any]:
            if transcript_id is not None:
                if text is not None:
                    raise OveError(
                        "invalid_input",
                        "Supply either text or transcript_id, not both.",
                        "A transcript already carries its own timing.",
                    )
                cues = service.cues_from_transcript(
                    transcript_id, max_characters=max_characters, max_duration=max_duration
                )
            elif text is not None:
                span = duration
                if span is None and asset_id is not None:
                    span = service.media_info(asset_id)["media"].get("video", {}).get("duration")
                if span is None:
                    raise OveError(
                        "invalid_input",
                        "A duration is required to place supplied text on a timeline.",
                        "Pass duration, or pass asset_id so the length can be read from it.",
                    )
                cues = service.cues_from_text(
                    text,
                    float(span),
                    start,
                    max_characters=max_characters,
                    max_duration=max_duration,
                )
            else:
                raise OveError(
                    "invalid_input",
                    "Supply text or a transcript_id.",
                    "Transcribe first with transcribe_media, or pass the caption text directly.",
                )
            return _cues(cues, **_sidecar(cues, sidecar_format))

        return respond(build)

    @server.tool(title="Style captions", annotations=READ)
    def style_captions(
        cues: list[Any],
        style: CAPTION_STYLE = "modern",
        max_characters: int = 84,
        max_duration: float = 6.0,
        sidecar_format: Literal["srt", "vtt"] | None = None,
    ) -> Annotated[CallToolResult, Envelope[CaptionCuesOut]]:
        """Apply a caption style and re-split any cue that is too long or too slow to read.

        Styles are modern, lower-third, bold, minimal and highlight. The returned
        cues are what burn_captions will render. Nothing is rendered here.
        """
        from ove.domain.models import CaptionCue

        parsed = [CaptionCue.model_validate(cue) for cue in cues]
        styled = service.restyle_captions(parsed, style, max_characters, max_duration)
        return respond(lambda: _cues(styled, style=style, **_sidecar(styled, sidecar_format)))

    @server.tool(title="Translate captions", annotations=READ)
    def translate_captions(
        cues: list[Any], target_language: str
    ) -> Annotated[CallToolResult, Envelope[TranslationOut]]:
        """Translate caption text into another language, keeping each cue's timing.

        Requires a configured translation provider; when none is installed the
        call fails with EXTERNAL_SERVICE_ERROR and an actionable message. Word-level
        timing is not transferred, so re-check timing after translating.
        """
        from ove.domain.models import CaptionCue

        parsed = [CaptionCue.model_validate(cue) for cue in cues]
        return respond(lambda: service.translate_captions(parsed, target_language))

    @server.tool(title="Burn captions into video", annotations=WRITE)
    def burn_captions(
        asset_id: str,
        cues: list[Any],
        style: CAPTION_STYLE = "modern",
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Render caption cues permanently into the picture.

        Cue times refer to the final timeline. Text is drawn with the installed
        font fallback, so review glyph coverage and line wrapping on the output.
        Cues must be ordered and non-overlapping. Queued, not rendered.
        """
        from ove.domain.models import CaptionCue

        parsed = [CaptionCue.model_validate(cue) for cue in cues]
        styled = service.restyle_captions(parsed, style)
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Captions(cues=styled, style=style)],
                export=_export(quality),
                note=f"Burn {len(styled)} caption cue(s) into {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Suggest a title", annotations=READ)
    def generate_title(
        subject: str,
        detail: str | None = None,
        audience: str | None = None,
        limit: int = 5,
    ) -> Annotated[CallToolResult, Envelope[TextSuggestionsOut]]:
        """Produce title options for a topic using built-in templates.

        These are deterministic templates, not model-generated copy: the response
        reports source="template" and model_generated=false. `detail` and
        `audience` unlock additional patterns.
        """
        return respond(lambda: _suggest("title", subject, detail, audience, limit))

    @server.tool(title="Suggest a hook", annotations=READ)
    def generate_hook(
        subject: str,
        detail: str | None = None,
        audience: str | None = None,
        limit: int = 5,
    ) -> Annotated[CallToolResult, Envelope[TextSuggestionsOut]]:
        """Produce opening-line options designed to hold attention.

        Template-based, not model-generated: the response reports
        source="template" and model_generated=false. Review before publishing.
        """
        return respond(lambda: _suggest("hook", subject, detail, audience, limit))

    @server.tool(title="Suggest a call to action", annotations=READ)
    def generate_cta(
        subject: str,
        detail: str | None = None,
        audience: str | None = None,
        limit: int = 5,
    ) -> Annotated[CallToolResult, Envelope[TextSuggestionsOut]]:
        """Produce closing call-to-action options such as follow, save or subscribe.

        Template-based, not model-generated: the response reports
        source="template" and model_generated=false. Review before publishing.
        """
        return respond(lambda: _suggest("cta", subject, detail, audience, limit))

    # ------------------------------------------------------- motion graphics

    @server.tool(title="Add an animated title", annotations=WRITE)
    def create_animated_title(
        asset_id: str,
        text: str,
        start: float,
        end: float,
        animation: ANIMATIONS = "fade",
        position: POSITIONS | None = None,
        accent_color: str = "0x00E5A0",
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Burn a headline with an entrance animation onto the video.

        Times refer to the final timeline. Omit `position` for the template's
        default placement. `accent_color` is a 0xRRGGBB value. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [
                    TextOverlay(
                        template="title",
                        text=text,
                        start=start,
                        end=end,
                        animation=animation,
                        position=position,
                        accent_color=accent_color,
                    )
                ],
                export=_export(quality),
                note=f"Animate a title on {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Add a lower third", annotations=WRITE)
    def create_lower_third(
        asset_id: str,
        text: str,
        detail: str | None = None,
        start: float = 0.0,
        end: float = 4.0,
        animation: ANIMATIONS = "slide_up",
        position: POSITIONS | None = None,
        accent_color: str = "0x00E5A0",
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Add a name-and-role banner along the bottom of the frame.

        `text` is the primary line and `detail` the smaller second line, for
        example a job title. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [
                    TextOverlay(
                        template="lower_third",
                        text=text,
                        detail=detail,
                        start=start,
                        end=end,
                        animation=animation,
                        position=position,
                        accent_color=accent_color,
                    )
                ],
                export=_export(quality),
                note=f"Add a lower third to {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Add a callout", annotations=WRITE)
    def create_callout(
        asset_id: str,
        text: str,
        detail: str | None = None,
        start: float = 0.0,
        end: float = 3.0,
        animation: ANIMATIONS = "pop",
        position: POSITIONS | None = None,
        accent_color: str = "0x00E5A0",
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Highlight a detail with a short emphasised label.

        Use for labels, warnings or annotations that should pull the eye for a
        moment. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [
                    TextOverlay(
                        template="callout",
                        text=text,
                        detail=detail,
                        start=start,
                        end=end,
                        animation=animation,
                        position=position,
                        accent_color=accent_color,
                    )
                ],
                export=_export(quality),
                note=f"Add a callout to {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Animate arbitrary text", annotations=WRITE)
    def create_text_animation(
        asset_id: str,
        text: str,
        template: TEMPLATES = "title",
        animation: ANIMATIONS = "typewriter",
        start: float = 0.0,
        end: float = 3.0,
        detail: str | None = None,
        position: POSITIONS | None = None,
        accent_color: str = "0x00E5A0",
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Place text using any template and animation combination.

        This is the general form of the other text tools; prefer the specific
        tools when they fit. `typewriter` is limited to short lines. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [
                    TextOverlay(
                        template=template,
                        text=text,
                        detail=detail,
                        start=start,
                        end=end,
                        animation=animation,
                        position=position,
                        accent_color=accent_color,
                    )
                ],
                export=_export(quality),
                note=f"Animate text on {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Animate a logo overlay", annotations=WRITE)
    def create_logo_animation(
        asset_id: str,
        logo_asset_id: str,
        position: POSITIONS = "top_right",
        scale: float = 0.18,
        opacity: float = 1.0,
        animation: OVERLAY_ANIMATIONS = "fade",
        start: float = 0.0,
        end: float | None = None,
        margin: int = 24,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Composite an image or video overlay, typically a logo, over the picture.

        `scale` is the overlay width as a fraction of the base width and `margin`
        the inset in pixels. `end` defaults to the end of the base video. The
        overlay asset itself is never modified. Queued, not rendered.
        """
        return respond(
            lambda: _job(
                service,
                [asset_id, logo_asset_id],
                OverlaySpec(
                    overlay_asset_id=logo_asset_id,
                    position=position,
                    margin=margin,
                    scale=scale,
                    opacity=opacity,
                    start=start,
                    end=end,
                    animation=animation,
                ),
                export=_export(quality),
                note=f"Composite an overlay onto {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Add a progress bar", annotations=WRITE)
    def create_progress_bar(
        asset_id: str,
        position: Literal["bottom", "top"] = "bottom",
        color: str = "0x00E5A0",
        thickness: int = 8,
        margin: int = 0,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Draw a bar that fills in step with playback across the whole clip.

        Useful for countdowns, quizzes and list videos. `color` is a 0xRRGGBB
        value, `thickness` and `margin` are pixels. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [ProgressBar(position=position, color=color, thickness=thickness, margin=margin)],
                export=_export(quality),
                note=f"Add a progress bar to {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Create a transition", annotations=WRITE)
    def create_transition(
        asset_ids: list[str],
        transition: TRANSITIONS = "fade",
        transition_duration: float = 0.5,
        normalize: bool = True,
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Join clips with a cross-fade, wipe or geometric transition between them.

        The clips play in the order given; each join overlaps by
        `transition_duration` seconds, shortening the total. Audio is cross-faded
        to match. Queued, not rendered.
        """
        if transition == "none":
            raise OveError(
                "invalid_input",
                "Choose a transition other than none, or use video_merge instead.",
            )
        return respond(
            lambda: _job(
                service,
                asset_ids,
                ConcatSpec(
                    transition=transition,
                    transition_duration=transition_duration,
                    normalize=normalize,
                ),
                export=_export(quality),
                note=f"Transition between {len(asset_ids)} clips",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Animate a zoom", annotations=WRITE)
    def create_zoom_animation(
        asset_id: str,
        start_zoom: float = 1.0,
        end_zoom: float = 1.4,
        focus: Literal["center", "top", "bottom", "left", "right"] = "center",
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Push in or pull out smoothly across the whole clip.

        Zoom factors run 1.0 to 4.0; the frame is cropped to the zoomed region, so
        confirm the subject stays in view. `focus` biases the zoom toward an edge.
        Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [Zoom(start_zoom=start_zoom, end_zoom=end_zoom, focus=focus)],
                export=_export(quality),
                note=f"Animate a zoom on {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    @server.tool(title="Add a social handle", annotations=WRITE)
    def create_social_overlay(
        asset_id: str,
        handle: str,
        start: float = 0.0,
        end: float = 5.0,
        position: POSITIONS | None = None,
        animation: ANIMATIONS = "fade",
        accent_color: str = "0x00E5A0",
        quality: Quality = "high",
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Display an account handle, for example @studio, as a persistent overlay.

        Omit `position` for the template default. Queued, not rendered.
        """
        return respond(
            lambda: _render(
                service,
                asset_id,
                [
                    TextOverlay(
                        template="social_handle",
                        text=handle,
                        start=start,
                        end=end,
                        animation=animation,
                        position=position,
                        accent_color=accent_color,
                    )
                ],
                export=_export(quality),
                note=f"Add a social overlay to {asset_id}",
                idempotency_key=idempotency_key,
                wait_seconds=wait_seconds,
            )
        )

    # ---------------------------------------------------------------- formats

    @server.tool(title="List formats", annotations=READ)
    def list_formats(
        category: str | None = None,
    ) -> Annotated[CallToolResult, Envelope[FormatListOut]]:
        """List the installed delivery presets, optionally filtered by category.

        Categories are platform, resolution, codec and quality. Platform presets
        are advisory editing profiles, not verified upload limits.
        """
        return respond(lambda: service.list_formats(category))

    @server.tool(title="Get a format", annotations=READ)
    def get_format(format_id: str) -> Annotated[CallToolResult, Envelope[FormatOut]]:
        """Read one delivery preset by identifier, including its exact settings."""
        return respond(lambda: service.get_format(format_id))

    @server.tool(title="Apply a format", annotations=WRITE)
    def apply_format(
        asset_id: str,
        format_ids: list[str],
        overrides: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Render an asset to a delivery preset, resizing only when the canvas differs.

        Presets compose; when two disagree on a field you must resolve it with an
        explicit `overrides` value. Queued, not rendered.
        """
        return respond(
            lambda: service.apply_format(
                asset_id, format_ids, overrides, idempotency_key, wait_seconds
            )
        )

    @server.tool(title="Validate a format", annotations=READ)
    def validate_format(
        format_ids: list[str],
        overrides: dict[str, Any] | None = None,
        asset_id: str | None = None,
    ) -> Annotated[CallToolResult, Envelope[FormatValidationOut]]:
        """Check that a preset composition is coherent and this engine can encode it.

        Pass `asset_id` to also compare the source aspect ratio and duration against
        the target. Returns per-check results rather than raising, so a mismatch is
        reported, not thrown.
        """
        return respond(lambda: service.validate_format(format_ids, overrides, asset_id))

    @server.tool(title="Export to a file", annotations=WRITE)
    def export_media(
        asset_id: str,
        format_ids: list[str],
        overrides: dict[str, Any] | None = None,
        output_path: str | None = None,
        idempotency_key: str | None = None,
        wait_seconds: float = 90.0,
    ) -> Annotated[CallToolResult, Envelope[ExportOut]]:
        """Render to a delivery preset and optionally copy the result to a path.

        This call waits (default 90 seconds, capped by the server budget) because a
        file copy cannot begin before validation passes. With `output_path` an
        existing file is never overwritten; the copy fails instead. Without it, the
        artifact path in the job handle is returned.
        """
        return respond(
            lambda: service.export_media(
                asset_id, format_ids, overrides, output_path, idempotency_key, wait_seconds
            )
        )

    # --------------------------------------------------------------- projects

    @server.tool(title="Create a project", annotations=WRITE)
    def create_project(
        name: str, asset_ids: list[str] | None = None
    ) -> Annotated[CallToolResult, Envelope[ProjectOut]]:
        """Create a named project holding references to imported assets.

        Assets are referenced, never copied, and duplicates are collapsed. A
        project starts at revision 1.
        """
        return respond(lambda: _project(service.create_project(name, asset_ids)))

    @server.tool(title="Get a project", annotations=READ)
    def get_project(
        project_id: str, revision: int | None = None
    ) -> Annotated[CallToolResult, Envelope[ProjectOut]]:
        """Read a project's current state, or an immutable earlier revision.

        Omit `revision` for the latest. Historical revisions are retained, so an
        earlier edit decision list can always be recovered.
        """
        return respond(lambda: _project(service.get_project(project_id, revision)))

    @server.tool(title="Update a project", annotations=WRITE)
    def update_project(
        project_id: str,
        expected_revision: int,
        name: str | None = None,
        asset_ids: list[str] | None = None,
    ) -> Annotated[CallToolResult, Envelope[ProjectOut]]:
        """Change a project's name and/or asset list, recording a new revision.

        `expected_revision` must match the current revision; a stale value fails
        with CONFLICT so concurrent edits are never silently lost. Supply at least
        one change.
        """
        return respond(
            lambda: _project(service.update_project(project_id, expected_revision, name, asset_ids))
        )

    @server.tool(title="List projects", annotations=READ)
    def list_projects(
        limit: int = 20, offset: int = 0
    ) -> Annotated[CallToolResult, Envelope[ProjectListOut]]:
        """List projects newest first with a summary of each."""
        return respond(lambda: service.list_projects(limit, offset))

    @server.tool(title="Add an asset to a project", annotations=WRITE)
    def add_asset(
        project_id: str, expected_revision: int, asset_id: str
    ) -> Annotated[CallToolResult, Envelope[ProjectOut]]:
        """Append one asset to a project, recording a new revision.

        Adding an asset that is already present fails with INVALID_INPUT rather
        than creating a no-op revision. A stale `expected_revision` fails with CONFLICT.
        """
        return respond(lambda: _project(service.add_asset(project_id, expected_revision, asset_id)))

    @server.tool(title="Remove an asset from a project", annotations=WRITE)
    def remove_asset(
        project_id: str, expected_revision: int, asset_id: str
    ) -> Annotated[CallToolResult, Envelope[ProjectOut]]:
        """Detach one asset from a project, recording a new revision.

        The asset itself is not deleted; only the project's reference is dropped.
        """
        return respond(
            lambda: _project(service.remove_asset(project_id, expected_revision, asset_id))
        )

    @server.tool(title="List assets", annotations=READ)
    def list_assets(
        project_id: str | None = None, limit: int = 20, offset: int = 0
    ) -> Annotated[CallToolResult, Envelope[AssetListOut]]:
        """List imported assets, either across the library or within one project."""
        return respond(lambda: service.list_assets(project_id, limit, offset))

    @server.tool(title="Create a review preview", annotations=WRITE)
    def create_preview(
        asset_id: str,
        max_height: int = 480,
        start: float | None = None,
        end: float | None = None,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> Annotated[CallToolResult, Envelope[JobHandle]]:
        """Render a small, fast proxy of an asset for review.

        Downscales to at most `max_height` and can cover just a section. Use this to
        check an edit cheaply before committing to a full render. Supply both `start`
        and `end`, or neither. Queued, not rendered.
        """
        return respond(
            lambda: service.create_preview(
                asset_id, max_height, start, end, idempotency_key, wait_seconds
            )
        )

    @server.tool(title="Get job status", annotations=READ)
    def get_job_status(job_id: str) -> Annotated[CallToolResult, Envelope[JobStatus]]:
        """Read a job's state, stage and any validated artifacts.

        Only state 'succeeded' means a checked output file exists. 'validation_failed'
        means the engine produced a file that did not meet the plan's promise, and it
        was withheld. Progress is reported by stage, not by measured percentage.
        """
        return respond(lambda: service.job_status(job_id))

    @server.tool(title="Cancel a job", annotations=WRITE)
    def cancel_job(job_id: str) -> Annotated[CallToolResult, Envelope[JobStatus]]:
        """Request cancellation of a queued or running job.

        A queued job is cancelled at once. A running job stops when the worker next
        observes the request, so the returned state may still be 'cancelling'.
        """
        return respond(lambda: service.cancel_job(job_id))

    # ----------------------------------------------------------------- canva

    @server.tool(title="Connect to Canva", annotations=REMOTE)
    def canva_connect(
        action: Literal["start", "complete", "status", "disconnect"] = "status",
        code: str | None = None,
        state: str | None = None,
    ) -> Annotated[CallToolResult, Envelope[CanvaConnectOut]]:
        """Run the Canva authorization flow. No token or secret is ever returned.

        Use action 'start' to get an authorization URL, then 'complete' with the
        `code` from the redirect to finish. 'status' reports the current connection
        and 'disconnect' erases the stored credentials. Fails with
        EXTERNAL_SERVICE_ERROR when the integration is not configured on this server.
        """

        def connect() -> dict[str, Any]:
            result = service.canva_connect(action, code, state)
            return {"connected": False, **result}

        return respond(connect)

    @server.tool(title="List Canva designs", annotations=REMOTE)
    def canva_list_designs(
        query: str | None = None, limit: int = 20, continuation: str | None = None
    ) -> Annotated[CallToolResult, Envelope[CanvaDesignListOut]]:
        """List designs in the connected Canva account, optionally filtered by a search term.

        Requires an authorized connection. Page through results with the returned
        `continuation` token.
        """
        return respond(lambda: service.canva_list_designs(query, limit, continuation))

    @server.tool(title="Get a Canva design", annotations=REMOTE)
    def canva_get_design(design_id: str) -> Annotated[CallToolResult, Envelope[CanvaDesignOut]]:
        """Read metadata for one Canva design, including its temporary edit and view URLs.

        Requires an authorized connection. URLs are short-lived; fetch them again
        rather than storing them.
        """
        return respond(lambda: service.canva_get_design(design_id))

    @server.tool(title="Upload an asset to Canva", annotations=REMOTE)
    def canva_upload_asset(
        asset_id: str | None = None, path: str | None = None, name: str | None = None
    ) -> Annotated[CallToolResult, Envelope[CanvaUploadOut]]:
        """Upload a local asset into the connected Canva account and confirm it arrived.

        Supply an imported `asset_id`, or an allowlisted `path`. The upload is
        verified by polling Canva's job before success is reported. Note that
        uploading is not the same as placing the asset in a design: the response
        reports inserted_into_design=false.
        """
        return respond(lambda: service.canva_upload_asset(asset_id, path, name))

    @server.tool(title="Create a Canva design", annotations=REMOTE)
    def canva_create_design(
        design_type: CanvaDesignTypeInput | None = None,
        title: str | None = None,
        asset_id: str | None = None,
        copy_design_id: str | None = None,
        brand_template_id: str | None = None,
    ) -> Annotated[CallToolResult, Envelope[CanvaDesignOut]]:
        """Create a Canva design from a preset type, a custom size, an asset, a copy or a template.

        Supply exactly one starting point: `design_type` (preset name or custom
        width/height), `asset_id`, `copy_design_id` or `brand_template_id`.
        A design created through the API is deleted after 7 days if never edited.
        """
        payload = design_type.model_dump() if design_type is not None else None
        return respond(
            lambda: service.canva_create_design(
                payload, title, asset_id, copy_design_id, brand_template_id
            )
        )

    @server.tool(title="Edit a Canva design", annotations=REMOTE)
    def canva_edit_design(
        design_id: str, operations: list[str] | None = None
    ) -> Annotated[CallToolResult, Envelope[CanvaDesignOut]]:
        """Attempt to change elements inside an existing Canva design.

        Not available. Canva exposes element-level editing only through its
        interactive Design Editing API, which requires an editor session; no REST
        endpoint performs it. This tool therefore always fails with
        UNSUPPORTED_FORMAT and returns the verified alternative, rather than
        reporting an edit that did not happen. Use canva_create_design,
        canva_upload_asset and canva_export_design, then finish in the Canva editor.
        """
        request = {"design_id": design_id, "operations": list(operations or [])}
        return respond(lambda: service.canva_edit_design(request))

    @server.tool(title="Export a Canva design", annotations=REMOTE)
    def canva_export_design(
        design_id: str,
        format: str = "png",
        quality: str | None = None,
        register_assets: bool = True,
    ) -> Annotated[CallToolResult, Envelope[CanvaExportOut]]:
        """Export a Canva design to files and register them as local assets.

        `quality` applies to jpg and png only. With `register_assets` each exported
        file is imported into local storage so it can be edited here; the design
        itself is unchanged.
        """
        return respond(
            lambda: service.canva_export_design(design_id, format, quality, register_assets)
        )

    # -------------------------------------------------------------- resources

    @server.resource("ove://capabilities")
    def capabilities_resource() -> str:
        return json.dumps(service.capabilities(), indent=2, default=str)

    @server.resource("ove://schemas/plan/1")
    def plan_schema_resource() -> str:
        return json.dumps(PlanRequest.model_json_schema(), indent=2)

    return server
