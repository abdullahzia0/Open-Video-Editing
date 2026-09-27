"""Versioned, serializable contracts. No engine or MCP imports belong here."""

import math
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Effect = Literal[
    "timing",
    "geometry",
    "lighting",
    "color",
    "denoise",
    "sharpen",
    "text",
    "motion",
    "audio",
    "format",
]

#: Caption presentation styles the ASS composer can render.
CaptionStyle = Literal["modern", "lower-third", "bold", "minimal", "highlight"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def _plain_text(value: str) -> str:
    if any(ord(character) < 32 and character != "\n" for character in value):
        raise ValueError("text cannot contain control characters")
    return value


# --------------------------------------------------------------------------------------
# Temporal and geometric operations
# --------------------------------------------------------------------------------------


class TimeRange(Model):
    """Half-open ``[start, end)`` interval in seconds."""

    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be greater than start; interval is [start, end)")
        return self


class Trim(Model):
    type: Literal["trim"] = "trim"
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be greater than start; interval is [start, end)")
        return self


class Cut(Model):
    """Remove the listed intervals and join what remains, rippling the timeline."""

    type: Literal["cut"] = "cut"
    ranges: list[TimeRange] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        ordered = sorted(self.ranges, key=lambda item: item.start)
        if any(a.end > b.start for a, b in zip(ordered, ordered[1:], strict=False)):
            raise ValueError("cut ranges must not overlap")
        self.ranges = ordered
        return self


class Resize(Model):
    type: Literal["resize"] = "resize"
    width: int = Field(ge=2, le=7680, multiple_of=2)
    height: int = Field(ge=2, le=7680, multiple_of=2)
    fit: Literal["pad", "crop", "stretch"] = "pad"


class Upscale(Model):
    """Conventional resampling upscale. Neural super-resolution is not offered."""

    type: Literal["upscale"] = "upscale"
    width: int = Field(ge=2, le=7680, multiple_of=2)
    height: int = Field(ge=2, le=7680, multiple_of=2)
    fit: Literal["pad", "crop", "stretch"] = "pad"
    sharpen: float = Field(default=0.0, ge=0, le=1.5)


class Crop(Model):
    type: Literal["crop"] = "crop"
    x: int = Field(ge=0, multiple_of=2)
    y: int = Field(ge=0, multiple_of=2)
    width: int = Field(ge=2, multiple_of=2)
    height: int = Field(ge=2, multiple_of=2)


class Rotate(Model):
    type: Literal["rotate"] = "rotate"
    degrees: Literal[90, 180, 270]


class Flip(Model):
    type: Literal["flip"] = "flip"
    axis: Literal["horizontal", "vertical"]


class Speed(Model):
    type: Literal["speed"] = "speed"
    factor: float = Field(ge=0.25, le=4)


class Freeze(Model):
    """Hold the frame at ``at`` for ``duration`` seconds, splicing the timeline.

    Audio is silent across the held frame; the original audio resumes afterwards.
    """

    type: Literal["freeze"] = "freeze"
    at: float = Field(ge=0)
    duration: float = Field(gt=0, le=120)


class Zoom(Model):
    """Linear zoom/pan between two zoom factors."""

    type: Literal["zoom"] = "zoom"
    start_zoom: float = Field(default=1.0, ge=1, le=4)
    end_zoom: float = Field(default=1.4, ge=1, le=4)
    focus: Literal["center", "top", "bottom", "left", "right"] = "center"


class Stabilize(Model):
    """Two-pass video stabilization. Cropping or border filling is expected."""

    type: Literal["stabilize"] = "stabilize"
    smoothing: int = Field(default=15, ge=1, le=60)
    crop: Literal["black", "keep"] = "black"
    zoom: float = Field(default=0, ge=-1, le=10)


# --------------------------------------------------------------------------------------
# Photometric operations
# --------------------------------------------------------------------------------------


class Lighting(Model):
    type: Literal["lighting"] = "lighting"
    brightness: float = Field(default=0, ge=-0.3, le=0.3)
    contrast: float = Field(default=1, ge=0.5, le=1.5)
    gamma: float = Field(default=1, ge=0.5, le=2)


class Color(Model):
    type: Literal["color"] = "color"
    saturation: float = Field(default=1, ge=0, le=2)


class ColorGrade(Model):
    """Apply a named creative look. ``intensity`` blends toward the source."""

    type: Literal["color_grade"] = "color_grade"
    look: Literal["neutral", "warm", "cool", "cinematic", "vintage", "noir", "vivid", "faded"] = (
        "neutral"
    )
    intensity: float = Field(default=1.0, ge=0, le=1)


class Denoise(Model):
    type: Literal["denoise"] = "denoise"
    strength: float = Field(default=2, ge=0.1, le=6)


class Sharpen(Model):
    type: Literal["sharpen"] = "sharpen"
    amount: float = Field(default=0.5, ge=0.1, le=1.5)


# --------------------------------------------------------------------------------------
# Text and motion-graphic operations
# --------------------------------------------------------------------------------------


class Title(Model):
    """Simple stable title. Prefer :class:`TextOverlay` for new motion work."""

    type: Literal["title"] = "title"
    text: str = Field(min_length=1, max_length=200)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    style: Literal["modern", "lower-third"] = "modern"
    animated: bool = False

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("title end must be after start")
        _plain_text(self.text)
        return self


class TextOverlay(Model):
    """Template-driven text or motion graphic burned onto the timeline."""

    type: Literal["text_overlay"] = "text_overlay"
    template: Literal[
        "title", "lower_third", "callout", "social_handle", "cta", "hook", "caption"
    ] = "title"
    text: str = Field(min_length=1, max_length=200)
    detail: str | None = Field(default=None, max_length=200)
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    position: (
        Literal[
            "center",
            "top",
            "bottom",
            "top_left",
            "top_right",
            "bottom_left",
            "bottom_right",
        ]
        | None
    ) = Field(default=None, description="Omit to use the template's default placement.")
    animation: Literal[
        "none", "fade", "slide_up", "slide_down", "slide_left", "pop", "typewriter"
    ] = "none"
    accent_color: str = Field(default="0x00E5A0", pattern=r"^0x[0-9A-Fa-f]{6}$")

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("overlay end must be after start")
        _plain_text(self.text)
        if self.detail is not None:
            _plain_text(self.detail)
        return self


class ProgressBar(Model):
    """Time-driven progress bar drawn over the frame."""

    type: Literal["progress_bar"] = "progress_bar"
    position: Literal["bottom", "top"] = "bottom"
    color: str = Field(default="0x00E5A0", pattern=r"^0x[0-9A-Fa-f]{6}$")
    thickness: int = Field(default=8, ge=2, le=200)
    margin: int = Field(default=0, ge=0, le=400)


class CaptionCue(Model):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("cue end must be after start")
        _plain_text(self.text)
        return self


class Captions(Model):
    type: Literal["captions"] = "captions"
    cues: list[CaptionCue] = Field(min_length=1, max_length=5000)
    style: CaptionStyle = "modern"

    @model_validator(mode="after")
    def nonoverlap(self) -> Self:
        if any(a.end > b.start for a, b in zip(self.cues, self.cues[1:], strict=False)):
            raise ValueError("caption cues must be ordered and non-overlapping")
        return self


Operation = Annotated[
    Trim
    | Cut
    | Resize
    | Upscale
    | Crop
    | Rotate
    | Flip
    | Speed
    | Freeze
    | Zoom
    | Stabilize
    | Lighting
    | Color
    | ColorGrade
    | Denoise
    | Sharpen
    | Title
    | TextOverlay
    | ProgressBar
    | Captions,
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------------------
# Audio operations used by the audio job kinds
# --------------------------------------------------------------------------------------


class AudioNormalize(Model):
    type: Literal["audio_normalize"] = "audio_normalize"
    target_lufs: float = Field(default=-16.0, ge=-40, le=-5)
    true_peak_db: float = Field(default=-1.5, ge=-9, le=0)
    loudness_range: float = Field(default=11.0, ge=1, le=50)


class AudioFade(Model):
    type: Literal["audio_fade"] = "audio_fade"
    fade_in: float = Field(default=0.0, ge=0, le=60)
    fade_out: float = Field(default=0.0, ge=0, le=60)
    curve: Literal["tri", "qsin", "exp", "log", "par"] = "tri"


AudioOperation = Annotated[AudioNormalize | AudioFade, Field(discriminator="type")]


# --------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------


class Intent(Model):
    request: str = Field(min_length=1, max_length=4000)
    allowed_effects: set[Effect]


class ExportSpec(Model):
    container: Literal["mp4", "mkv", "webm", "mov"] = "mp4"
    video_codec: Literal["h264", "hevc", "vp9", "copy"] = "h264"
    crf: int = Field(default=18, ge=0, le=35)
    encoder_preset: Literal["ultrafast", "fast", "medium", "slow"] = "medium"
    audio: Literal["copy", "aac", "opus", "drop"] = "copy"
    audio_bitrate_kbps: int = Field(default=192, ge=32, le=320)
    width: int | None = Field(default=None, ge=2, le=7680, multiple_of=2)
    height: int | None = Field(default=None, ge=2, le=7680, multiple_of=2)
    fit: Literal["pad", "crop", "stretch"] = "pad"

    @model_validator(mode="after")
    def dimensions(self) -> Self:
        if (self.width is None) != (self.height is None):
            raise ValueError("width and height must be supplied together")
        return self


class PlanRequest(Model):
    """A single-source render plan. ``project_id`` is optional for direct asset edits."""

    schema_version: Literal["1"] = "1"
    project_id: str | None = None
    revision: int | None = Field(default=None, ge=1)
    asset_id: str
    operations: list[Operation] = Field(default_factory=list, max_length=100)
    intent: Intent
    export: ExportSpec = Field(default_factory=ExportSpec)

    @model_validator(mode="after")
    def project_pair(self) -> Self:
        if (self.project_id is None) != (self.revision is None):
            raise ValueError("project_id and revision must be supplied together")
        return self


# --------------------------------------------------------------------------------------
# Multi-input media job specs
# --------------------------------------------------------------------------------------


class ConcatSpec(Model):
    """Join several videos in order, optionally cross-fading between them."""

    type: Literal["concat"] = "concat"
    transition: Literal[
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
    ] = "none"
    transition_duration: float = Field(default=0.5, ge=0.1, le=5)
    normalize: bool = True


class SplitSpec(Model):
    """Export consecutive segments of one source as separate artifacts."""

    type: Literal["split"] = "split"
    boundaries: list[float] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if any(value <= 0 for value in self.boundaries):
            raise ValueError("boundaries must be positive seconds")
        if self.boundaries != sorted(set(self.boundaries)):
            raise ValueError("boundaries must be unique and strictly increasing")
        return self


class FrameSpec(Model):
    """Export one still image from a video."""

    type: Literal["frame"] = "frame"
    at: float = Field(ge=0)
    format: Literal["jpg", "png", "webp"] = "jpg"
    quality: int = Field(default=2, ge=1, le=31)
    width: int | None = Field(default=None, ge=16, le=7680, multiple_of=2)


class AudioExtractSpec(Model):
    type: Literal["audio_extract"] = "audio_extract"
    container: Literal["m4a", "mp3", "wav", "opus", "webm"] = "m4a"
    bitrate_kbps: int = Field(default=192, ge=32, le=512)
    sample_rate: int | None = Field(default=None, ge=8000, le=192000)
    channels: Literal[1, 2] | None = None


class AudioProcessSpec(Model):
    """Modify the audio of one asset while preserving its video, if any."""

    type: Literal["audio_process"] = "audio_process"
    container: Literal["m4a", "mp3", "wav", "opus", "webm", "keep"] = "keep"
    normalize: AudioNormalize | None = None
    fade: AudioFade | None = None
    trim: TimeRange | None = None
    remove: bool = False

    @model_validator(mode="after")
    def at_least_one(self) -> Self:
        if not any((self.normalize, self.fade, self.trim, self.remove)):
            raise ValueError("supply at least one of normalize, fade, trim or remove")
        if self.remove and any((self.normalize, self.fade, self.trim)):
            raise ValueError("remove cannot be combined with other audio operations")
        return self


class AudioReplaceSpec(Model):
    """Replace a video's audio with another asset's audio."""

    type: Literal["audio_replace"] = "audio_replace"
    audio_asset_id: str
    mix_with_original: float = Field(default=0.0, ge=0, le=1)
    loop: bool = True


class AudioMixSpec(Model):
    """Mix several audio streams. With ``video_asset_id`` the mix is muxed onto video."""

    type: Literal["audio_mix"] = "audio_mix"
    weights: list[float] | None = Field(default=None, max_length=50)
    duration: Literal["longest", "shortest", "first"] = "longest"
    video_asset_id: str | None = None
    container: Literal["m4a", "mp3", "wav", "opus", "webm"] = "m4a"
    bitrate_kbps: int = Field(default=192, ge=32, le=512)

    @model_validator(mode="after")
    def weights_match(self) -> Self:
        if self.weights is not None and any(weight < 0 or weight > 4 for weight in self.weights):
            raise ValueError("weights must be between 0 and 4")
        return self


class OverlaySpec(Model):
    """Composite a still image (typically a logo) onto a video with optional motion."""

    type: Literal["overlay"] = "overlay"
    overlay_asset_id: str
    position: Literal[
        "center",
        "top",
        "bottom",
        "top_left",
        "top_right",
        "bottom_left",
        "bottom_right",
    ] = "top_right"
    margin: int = Field(default=24, ge=0, le=400)
    scale: float = Field(default=0.18, gt=0, le=1)
    opacity: float = Field(default=1.0, gt=0, le=1)
    start: float = Field(default=0, ge=0)
    end: float | None = Field(default=None, gt=0)
    animation: Literal["none", "fade", "slide_in"] = "fade"

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end is not None and self.end <= self.start:
            raise ValueError("overlay end must be after start")
        return self


MediaJobSpec = Annotated[
    ConcatSpec
    | SplitSpec
    | FrameSpec
    | AudioExtractSpec
    | AudioProcessSpec
    | AudioReplaceSpec
    | AudioMixSpec
    | OverlaySpec,
    Field(discriminator="type"),
]


class MediaJobRequest(Model):
    """A multi-input or non-video job. ``export`` applies to video-producing kinds."""

    schema_version: Literal["1"] = "1"
    project_id: str | None = None
    revision: int | None = Field(default=None, ge=1)
    asset_ids: list[str] = Field(min_length=1, max_length=50)
    job: MediaJobSpec
    export: ExportSpec = Field(default_factory=ExportSpec)
    intent: Intent

    @model_validator(mode="after")
    def project_pair(self) -> Self:
        if (self.project_id is None) != (self.revision is None):
            raise ValueError("project_id and revision must be supplied together")
        return self


# --------------------------------------------------------------------------------------
# Probe and expectation models
# --------------------------------------------------------------------------------------


class MediaInfo(Model):
    duration: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    video_codec: str
    pixel_format: str
    frame_rate: str
    audio_codec: str | None = None
    audio_channels: int | None = None
    audio_sample_rate: int | None = None
    color_transfer: str | None = None
    color_space: str | None = None
    color_primaries: str | None = None
    color_range: str | None = None
    rotation: int = 0
    sample_aspect_ratio: str = "1:1"
    video_streams: int = 1
    audio_streams: int = 0
    other_streams: int = 0

    @property
    def frame_seconds(self) -> float:
        try:
            n, d = (int(x) for x in self.frame_rate.split("/"))
            value = d / n
            return value if math.isfinite(value) and value > 0 else 0.04
        except (ValueError, ZeroDivisionError):
            return 0.04


class AudioInfo(Model):
    duration: float = Field(ge=0)
    audio_codec: str
    channels: int = Field(gt=0)
    sample_rate: int = Field(gt=0)
    bit_rate: int | None = None


class ImageInfo(Model):
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    codec: str


class ProbedMedia(Model):
    """Kind-aware probe result stored on every imported asset."""

    kind: Literal["video", "audio", "image"]
    video: MediaInfo | None = None
    audio: AudioInfo | None = None
    image: ImageInfo | None = None

    @model_validator(mode="after")
    def populated(self) -> Self:
        expected = {"video": self.video, "audio": self.audio, "image": self.image}
        if expected[self.kind] is None:
            raise ValueError(f"{self.kind} probe is missing its detail block")
        return self

    @property
    def duration(self) -> float:
        if self.video is not None:
            return self.video.duration
        if self.audio is not None:
            return self.audio.duration
        return 0.0

    @property
    def has_video(self) -> bool:
        return self.video is not None

    @property
    def has_audio(self) -> bool:
        return (self.video is not None and bool(self.video.audio_codec)) or self.audio is not None


class ExpectedMedia(Model):
    """What a job promises to produce. ``count`` covers split jobs."""

    kind: Literal["video", "image", "audio"] = "video"
    count: int = Field(default=1, ge=1, le=200)
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    has_audio: bool | None = None
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None


class Check(Model):
    name: str
    passed: bool
    expected: str
    actual: str


class ValidationReport(Model):
    checks: list[Check]
    coverage: Literal["full-decode", "structural"] = "full-decode"
    warnings: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)
