"""Semantic effect validation and deterministic output expectations.

Two entry points exist: :func:`validate_plan` for single-source renders and
:func:`validate_media_job` for the multi-input and non-video job kinds. Both
enforce the declared intent scope before any work is queued.
"""

from ove.domain.errors import OveError
from ove.domain.models import (
    AudioExtractSpec,
    AudioMixSpec,
    AudioProcessSpec,
    AudioReplaceSpec,
    Captions,
    ConcatSpec,
    Crop,
    Cut,
    Effect,
    ExpectedMedia,
    ExportSpec,
    FrameSpec,
    Freeze,
    MediaInfo,
    MediaJobRequest,
    OverlaySpec,
    PlanRequest,
    ProbedMedia,
    ProgressBar,
    Resize,
    Rotate,
    Speed,
    SplitSpec,
    TextOverlay,
    Title,
    Trim,
    Upscale,
)
from ove.motion.ass import safe_text

#: Operation type -> the single intent effect it declares.
EFFECTS: dict[str, Effect] = {
    "trim": "timing",
    "cut": "timing",
    "speed": "timing",
    "freeze": "timing",
    "resize": "geometry",
    "upscale": "geometry",
    "crop": "geometry",
    "rotate": "geometry",
    "flip": "geometry",
    "stabilize": "geometry",
    "zoom": "motion",
    "progress_bar": "motion",
    "lighting": "lighting",
    "color": "color",
    "color_grade": "color",
    "denoise": "denoise",
    "sharpen": "sharpen",
    "title": "text",
    "text_overlay": "text",
    "captions": "text",
}

#: Effects a job kind declares on top of its operations.
JOB_EFFECTS: dict[str, set[Effect]] = {
    "concat": {"timing", "format"},
    "split": {"timing"},
    "frame": set(),
    "audio_extract": {"audio"},
    "audio_process": {"audio"},
    "audio_replace": {"audio"},
    "audio_mix": {"audio"},
    "overlay": {"motion"},
}

OVERLAY_OPERATIONS = (Title, TextOverlay, ProgressBar, Captions)

#: Container -> video codecs that container accepts without remuxing surprises.
CONTAINER_CODECS: dict[str, set[str]] = {
    "mp4": {"h264", "hevc"},
    "mov": {"h264", "hevc"},
    "mkv": {"h264", "hevc", "vp9"},
    "webm": {"vp9"},
}


def _require(condition: bool, code: str, message: str, action: str = "") -> None:
    if not condition:
        raise OveError(code, message, action)


def export_effects(export: ExportSpec) -> set[Effect]:
    """The intent effects an output profile necessarily declares."""
    effects: set[Effect] = set()
    if export.width is not None:
        effects.add("geometry")
    if (
        export.audio != "copy"
        or export.video_codec != "h264"
        or export.container not in {"mp4", "mkv"}
    ):
        effects.add("format")
    return effects


def _reject_unhandled_source(info: MediaInfo) -> None:
    if info.rotation or info.sample_aspect_ratio not in {"1:1", "0:1", "N/A"}:
        raise OveError(
            "unsupported_media",
            "Rotated metadata or non-square pixels need normalization.",
            "Normalize explicitly with a trusted editor before importing this release.",
        )
    if info.color_transfer in {"smpte2084", "arib-std-b67"}:
        raise OveError("unsupported_media", "HDR processing is not supported by this SDR pipeline.")
    if info.video_streams != 1 or info.audio_streams > 1 or info.other_streams:
        raise OveError(
            "unsupported_media",
            "Only one video and at most one audio stream are supported.",
            "Select and extract intended streams before import; no streams are silently dropped.",
        )


def _check_export(export: ExportSpec) -> None:
    codecs = CONTAINER_CODECS[export.container]
    if export.video_codec != "copy" and export.video_codec not in codecs:
        raise OveError(
            "unsupported_media",
            f"{export.video_codec} video cannot be stored in a {export.container} container.",
            "Choose a compatible container/codec pair or copy the source stream.",
        )
    if export.container == "webm" and export.audio not in {"drop", "opus"}:
        raise OveError(
            "incompatible_audio",
            "WebM output requires dropping the audio or encoding it as Opus.",
            "Set audio to drop or opus.",
        )
    if export.container in {"mp4", "mov"} and export.audio == "opus":
        raise OveError(
            "incompatible_audio",
            "Opus audio is not stored in this container by the bundled muxer.",
            "Use MKV or WebM for Opus audio, or select aac.",
        )


def validate_plan(request: PlanRequest, info: MediaInfo, max_pixels: int) -> ExpectedMedia:
    """Validate a single-source render plan and return its output expectation."""
    _reject_unhandled_source(info)
    if info.pixel_format not in {"yuv420p", "yuvj420p"} and "format" not in (
        request.intent.allowed_effects
    ):
        raise OveError(
            "scope_violation", "Converting source pixels to 8-bit 4:2:0 needs format scope."
        )

    width, height, duration = info.width, info.height, info.duration
    required: set[Effect] = {EFFECTS[operation.type] for operation in request.operations}
    if request.export.width is not None:
        required.add("geometry")
    if request.export.audio != "copy":
        required.add("format")
    if request.export.video_codec != "h264" or request.export.container not in {"mp4", "mkv"}:
        required.add("format")
    missing = required - request.intent.allowed_effects
    if missing:
        raise OveError("scope_violation", f"Operations exceed allowed effects: {sorted(missing)}")
    _check_export(request.export)

    overlay_started = False
    for index, operation in enumerate(request.operations):
        if isinstance(operation, OVERLAY_OPERATIONS):
            overlay_started = True
            if isinstance(operation, Captions):
                for cue in operation.cues:
                    if cue.end > duration + 0.001:
                        raise OveError(
                            "invalid_range", "Text extends past the current timeline duration."
                        )
                    safe_text(cue.text)
            else:
                if operation.end > duration + 0.001:
                    raise OveError(
                        "invalid_range", "Text extends past the current timeline duration."
                    )
                safe_text(operation.text)
                if isinstance(operation, TextOverlay) and operation.detail:
                    safe_text(operation.detail)
        elif overlay_started:
            raise OveError(
                "operation_order",
                "Place text and overlay operations after all video edits.",
                "Overlay times refer to the final edited timeline, not source time.",
            )

        if isinstance(operation, Cut):
            if index != 0:
                raise OveError(
                    "operation_order",
                    "Cut must be the first operation because it reorders the timeline.",
                )
            for interval in operation.ranges:
                if interval.end > duration + 0.001:
                    raise OveError(
                        "invalid_range", "Cut range extends past the current timeline duration."
                    )
            duration -= sum(interval.end - interval.start for interval in operation.ranges)
        elif isinstance(operation, Trim):
            if operation.end > duration + 0.001:
                raise OveError("invalid_range", "Trim extends past the current timeline duration.")
            duration = operation.end - operation.start
        elif isinstance(operation, Speed):
            duration /= operation.factor
        elif isinstance(operation, Freeze):
            if operation.at > duration + 0.001:
                raise OveError(
                    "invalid_range", "Freeze point is past the current timeline duration."
                )
            duration += operation.duration
        elif isinstance(operation, (Resize, Upscale)):
            width, height = operation.width, operation.height
        elif isinstance(operation, Crop):
            if operation.x + operation.width > width or operation.y + operation.height > height:
                raise OveError("invalid_crop", "Crop rectangle exceeds the current canvas.")
            width, height = operation.width, operation.height
        elif isinstance(operation, Rotate) and operation.degrees in {90, 270}:
            width, height = height, width
        if width * height > max_pixels:
            raise OveError("resource_limit", "Intermediate canvas exceeds the pixel budget.")
        if duration <= 0:
            raise OveError("invalid_range", "The edited timeline would be empty.")

    if request.export.width is not None and request.export.height is not None:
        width, height = request.export.width, request.export.height
    if width * height > max_pixels or width % 2 or height % 2:
        raise OveError("invalid_dimensions", "Output requires bounded, even dimensions.")

    temporal = any(
        isinstance(operation, (Trim, Speed, Freeze, Cut)) for operation in request.operations
    )
    if request.export.container == "mp4" and request.export.audio == "copy" and not temporal:
        if info.audio_codec not in {None, "aac", "mp3", "ac3", "eac3", "alac"}:
            raise OveError(
                "incompatible_audio",
                "Source audio cannot be copied into this MP4 profile.",
                "Choose MKV or explicitly allow format changes and select AAC audio.",
            )
    return ExpectedMedia(
        kind="video",
        duration=duration,
        width=width,
        height=height,
        has_audio=bool(info.audio_codec) and request.export.audio != "drop",
        container=request.export.container,
        video_codec=request.export.video_codec,
    )


def _expectation_for_job(
    request: MediaJobRequest, media: list[ProbedMedia], max_pixels: int
) -> ExpectedMedia:
    spec = request.job
    effects = JOB_EFFECTS[spec.type]
    missing = effects - request.intent.allowed_effects
    if missing:
        raise OveError("scope_violation", f"Job exceeds allowed effects: {sorted(missing)}")

    if isinstance(spec, ConcatSpec):
        _require(len(media) >= 2, "invalid_input", "Merging needs at least two video assets.")
        for item in media:
            _require(item.video is not None, "unsupported_media", "Every merged asset needs video.")
        first = media[0].video
        assert first is not None
        if not spec.normalize:
            for item in media[1:]:
                video = item.video
                assert video is not None
                if (video.width, video.height) != (first.width, first.height):
                    raise OveError(
                        "unsupported_media",
                        "Merged assets differ in size and normalization is disabled.",
                        "Enable normalize or export assets with identical dimensions.",
                    )
        overlap = (len(media) - 1) * spec.transition_duration if spec.transition != "none" else 0.0
        duration = sum(item.duration for item in media) - overlap
        _require(duration > 0, "invalid_input", "Transitions are longer than the merged footage.")
        width = request.export.width or first.width
        height = request.export.height or first.height
        return ExpectedMedia(
            kind="video",
            duration=duration,
            width=width,
            height=height,
            has_audio=any(item.has_audio for item in media),
            container=request.export.container,
            video_codec=request.export.video_codec,
        )

    if isinstance(spec, SplitSpec):
        video = media[0].video
        _require(video is not None, "unsupported_media", "Splitting requires a video asset.")
        assert video is not None
        for boundary in spec.boundaries:
            _require(
                boundary < video.duration,
                "invalid_range",
                "A split boundary lies at or beyond the end of the source.",
            )
        return ExpectedMedia(
            kind="video",
            count=len(spec.boundaries) + 1,
            width=request.export.width or video.width,
            height=request.export.height or video.height,
            has_audio=bool(video.audio_codec),
            container=request.export.container,
            video_codec=request.export.video_codec,
        )

    if isinstance(spec, FrameSpec):
        video = media[0].video
        _require(video is not None, "unsupported_media", "Frame extraction requires a video asset.")
        assert video is not None
        _require(
            spec.at <= video.duration + 0.001,
            "invalid_range",
            "The requested timestamp is past the end of the source.",
        )
        scale = (spec.width / video.width) if spec.width else 1.0
        return ExpectedMedia(
            kind="image",
            width=spec.width or video.width,
            height=max(2, int(video.height * scale)) if spec.width else video.height,
            container=spec.format,
        )

    if isinstance(spec, AudioExtractSpec):
        _require(media[0].has_audio, "no_audio", "This asset has no audio stream to extract.")
        return ExpectedMedia(
            kind="audio",
            duration=media[0].duration,
            has_audio=True,
            container=spec.container,
        )

    if isinstance(spec, AudioProcessSpec):
        _require(media[0].has_audio, "no_audio", "This asset has no audio stream to process.")
        duration = media[0].duration
        if spec.trim is not None:
            _require(
                spec.trim.end <= duration + 0.001,
                "invalid_range",
                "The audio trim range is past the end of the source.",
            )
            duration = spec.trim.end - spec.trim.start
        if spec.remove:
            kind = "audio" if not media[0].has_video else "video"
            return ExpectedMedia(
                kind=kind,
                duration=duration if kind == "audio" else None,
                has_audio=False,
                container=spec.container if spec.container != "keep" else None,
            )
        if media[0].has_video:
            return ExpectedMedia(
                kind="video",
                duration=None,
                width=request.export.width,
                height=request.export.height,
                has_audio=True,
                container=request.export.container,
            )
        return ExpectedMedia(
            kind="audio",
            duration=duration,
            has_audio=True,
            container=None if spec.container == "keep" else spec.container,
        )

    if isinstance(spec, AudioReplaceSpec):
        _require(media[0].video is not None, "no_video", "The base asset must contain video.")
        _require(
            len(media) == 2 and media[1].has_audio, "no_audio", "The replacement audio is missing."
        )
        base_duration, replacement = media[0].duration, media[1].duration
        duration = base_duration if spec.loop else min(base_duration, replacement)
        _require(duration > 0, "invalid_input", "The replacement produces an empty result.")
        return ExpectedMedia(
            kind="video",
            duration=duration,
            width=request.export.width or (media[0].video.width if media[0].video else None),
            height=request.export.height or (media[0].video.height if media[0].video else None),
            has_audio=True,
            container=request.export.container,
            video_codec=request.export.video_codec,
        )

    if isinstance(spec, AudioMixSpec):
        for item in media:
            _require(item.has_audio, "no_audio", "Every mixed asset must contain audio.")
        if spec.weights is not None:
            _require(
                len(spec.weights) == len(media),
                "invalid_input",
                "Supply one weight per mixed asset or omit weights entirely.",
            )
        durations = [item.duration for item in media]
        policy = {"longest": max, "shortest": min, "first": lambda values: values[0]}[spec.duration]
        duration = float(policy(durations))
        if spec.video_asset_id is not None:
            video_base = media[-1]
            _require(video_base.video is not None, "no_video", "The video asset must contain video.")
            assert video_base.video is not None
            return ExpectedMedia(
                kind="video",
                duration=video_base.duration,
                width=request.export.width or video_base.video.width,
                height=request.export.height or video_base.video.height,
                has_audio=True,
                container=request.export.container,
                video_codec=request.export.video_codec,
            )
        return ExpectedMedia(
            kind="audio", duration=duration, has_audio=True, container=spec.container
        )

    if isinstance(spec, OverlaySpec):
        overlay_base = media[0]
        _require(
            overlay_base.video is not None, "no_video", "Overlaying requires a video base asset."
        )
        _require(
            len(media) == 2 and (media[1].image is not None or media[1].video is not None),
            "unsupported_media",
            "The overlay asset must be an image or a video.",
        )
        assert overlay_base.video is not None
        _require(
            spec.start <= overlay_base.video.duration + 0.001,
            "invalid_range",
            "The overlay start is past the end of the base video.",
        )
        if spec.end is not None:
            _require(
                spec.end <= overlay_base.video.duration + 0.001,
                "invalid_range",
                "The overlay end is past the end of the base video.",
            )
        return ExpectedMedia(
            kind="video",
            duration=overlay_base.video.duration,
            width=request.export.width or overlay_base.video.width,
            height=request.export.height or overlay_base.video.height,
            has_audio=bool(overlay_base.video.audio_codec),
            container=request.export.container,
            video_codec=request.export.video_codec,
        )

    raise OveError("unsupported_job", f"Unhandled job specification: {spec.type}")


def validate_media_job(
    request: MediaJobRequest, media: list[ProbedMedia], max_pixels: int
) -> ExpectedMedia:
    """Validate a multi-input or non-video job and return its output expectation."""
    if len(media) != len(request.asset_ids):
        raise OveError("invalid_input", "Supply one probe result per referenced asset.")
    for item in media:
        if item.video is not None:
            _reject_unhandled_source(item.video)
    expectation = _expectation_for_job(request, media, max_pixels)
    for dimension in (expectation.width, expectation.height):
        if dimension is not None and (dimension % 2 or dimension > 7680):
            raise OveError("invalid_dimensions", "Output requires bounded, even dimensions.")
    if (
        expectation.width is not None
        and expectation.height is not None
        and expectation.width * expectation.height > max_pixels
    ):
        raise OveError("resource_limit", "Output canvas exceeds the pixel budget.")
    return expectation
