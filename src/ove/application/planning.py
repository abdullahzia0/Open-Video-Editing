"""Semantic effect validation and deterministic output expectations."""

from ove.domain.errors import OveError
from ove.domain.models import (
    Captions,
    Crop,
    ExpectedMedia,
    MediaInfo,
    PlanRequest,
    Resize,
    Rotate,
    Speed,
    Title,
    Trim,
)
from ove.motion.ass import safe_text

EFFECTS = {
    "trim": "timing",
    "resize": "geometry",
    "crop": "geometry",
    "rotate": "geometry",
    "flip": "geometry",
    "speed": "timing",
    "lighting": "lighting",
    "color": "color",
    "denoise": "denoise",
    "sharpen": "sharpen",
    "title": "text",
    "captions": "text",
}


def validate_plan(request: PlanRequest, info: MediaInfo, max_pixels: int) -> ExpectedMedia:
    if info.rotation or info.sample_aspect_ratio not in {"1:1", "0:1", "N/A"}:
        raise OveError(
            "unsupported_media",
            "Rotated metadata or non-square pixels need normalization.",
            "Normalize explicitly with a trusted editor before importing this release.",
        )
    if info.color_transfer in {"smpte2084", "arib-std-b67"}:
        raise OveError("unsupported_media", "HDR processing is not supported by this SDR pipeline.")
    if info.pixel_format not in {"yuv420p", "yuvj420p"}:
        if "format" not in request.intent.allowed_effects:
            raise OveError(
                "scope_violation", "Converting source pixels to 8-bit 4:2:0 needs format scope."
            )
    if info.video_streams != 1 or info.audio_streams > 1 or info.other_streams:
        raise OveError(
            "unsupported_media",
            "Only one video and at most one audio stream are supported.",
            "Select and extract intended streams before import; no streams are silently dropped.",
        )
    width, height, duration = info.width, info.height, info.duration
    text_started = False
    required = {EFFECTS[op.type] for op in request.operations}
    if request.export.width is not None:
        required.add("geometry")
    if request.export.audio != "copy":
        required.add("format")
    missing = required - request.intent.allowed_effects
    if missing:
        raise OveError("scope_violation", f"Operations exceed allowed effects: {sorted(missing)}")
    for operation in request.operations:
        if isinstance(operation, (Title, Captions)):
            text_started = True
            entries = [operation] if isinstance(operation, Title) else operation.cues
            for cue in entries:
                if cue.end > duration + 0.001:
                    raise OveError(
                        "invalid_range", "Text extends past the current timeline duration."
                    )
                safe_text(cue.text)
        elif text_started:
            raise OveError(
                "operation_order",
                "Place text overlays after all video edits.",
                "Text times refer to the final edited timeline, not source time.",
            )
        if isinstance(operation, Trim):
            if operation.end > duration + 0.001:
                raise OveError("invalid_range", "Trim extends past the current timeline duration.")
            duration = operation.end - operation.start
        elif isinstance(operation, Speed):
            duration /= operation.factor
        elif isinstance(operation, Resize):
            width, height = operation.width, operation.height
        elif isinstance(operation, Crop):
            if operation.x + operation.width > width or operation.y + operation.height > height:
                raise OveError("invalid_crop", "Crop rectangle exceeds the current canvas.")
            width, height = operation.width, operation.height
        elif isinstance(operation, Rotate) and operation.degrees in {90, 270}:
            width, height = height, width
        if width * height > max_pixels:
            raise OveError("resource_limit", "Intermediate canvas exceeds the pixel budget.")
    if request.export.width is not None and request.export.height is not None:
        width, height = request.export.width, request.export.height
    if width * height > max_pixels or width % 2 or height % 2:
        raise OveError("invalid_dimensions", "H.264 output requires bounded, even dimensions.")
    temporal = any(isinstance(op, (Trim, Speed)) for op in request.operations)
    if request.export.container == "mp4" and request.export.audio == "copy" and not temporal:
        if info.audio_codec not in {None, "aac", "mp3", "ac3", "eac3", "alac"}:
            raise OveError(
                "incompatible_audio",
                "Source audio cannot be copied into this MP4 profile.",
                "Choose MKV or explicitly allow format changes and select AAC audio.",
            )
    return ExpectedMedia(
        duration=duration,
        width=width,
        height=height,
        has_audio=bool(info.audio_codec) and request.export.audio != "drop",
    )
