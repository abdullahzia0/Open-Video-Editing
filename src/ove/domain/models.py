"""Versioned, serializable contracts. No engine or MCP imports belong here."""

import math
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Effect = Literal["timing", "geometry", "lighting", "color", "denoise", "sharpen", "text", "format"]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Trim(Model):
    type: Literal["trim"] = "trim"
    start: float = Field(ge=0)
    end: float = Field(gt=0)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("end must be greater than start; interval is [start, end)")
        return self


class Resize(Model):
    type: Literal["resize"] = "resize"
    width: int = Field(ge=2, le=7680, multiple_of=2)
    height: int = Field(ge=2, le=7680, multiple_of=2)
    fit: Literal["pad", "crop", "stretch"] = "pad"


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


class Lighting(Model):
    type: Literal["lighting"] = "lighting"
    brightness: float = Field(default=0, ge=-0.3, le=0.3)
    contrast: float = Field(default=1, ge=0.5, le=1.5)
    gamma: float = Field(default=1, ge=0.5, le=2)


class Color(Model):
    type: Literal["color"] = "color"
    saturation: float = Field(default=1, ge=0, le=2)


class Denoise(Model):
    type: Literal["denoise"] = "denoise"
    strength: float = Field(default=2, ge=0.1, le=6)


class Sharpen(Model):
    type: Literal["sharpen"] = "sharpen"
    amount: float = Field(default=0.5, ge=0.1, le=1.5)


class Title(Model):
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
        if any(ord(c) < 32 and c != "\n" for c in self.text):
            raise ValueError("text cannot contain control characters")
        return self


class CaptionCue(Model):
    start: float = Field(ge=0)
    end: float = Field(gt=0)
    text: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("cue end must be after start")
        if any(ord(c) < 32 and c != "\n" for c in self.text):
            raise ValueError("text cannot contain control characters")
        return self


class Captions(Model):
    type: Literal["captions"] = "captions"
    cues: list[CaptionCue] = Field(min_length=1, max_length=5000)
    style: Literal["modern", "lower-third"] = "modern"

    @model_validator(mode="after")
    def nonoverlap(self) -> Self:
        if any(a.end > b.start for a, b in zip(self.cues, self.cues[1:], strict=False)):
            raise ValueError("caption cues must be ordered and non-overlapping")
        return self


Operation = Annotated[
    Trim
    | Resize
    | Crop
    | Rotate
    | Flip
    | Speed
    | Lighting
    | Color
    | Denoise
    | Sharpen
    | Title
    | Captions,
    Field(discriminator="type"),
]


class Intent(Model):
    request: str = Field(min_length=1, max_length=4000)
    allowed_effects: set[Effect]


class ExportSpec(Model):
    container: Literal["mp4", "mkv"] = "mp4"
    video_codec: Literal["h264"] = "h264"
    crf: int = Field(default=18, ge=0, le=35)
    encoder_preset: Literal["ultrafast", "fast", "medium", "slow"] = "medium"
    audio: Literal["copy", "aac", "drop"] = "copy"
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
    schema_version: Literal["1"] = "1"
    project_id: str
    revision: int = Field(ge=1)
    asset_id: str
    operations: list[Operation] = Field(default_factory=list, max_length=100)
    intent: Intent
    export: ExportSpec = Field(default_factory=ExportSpec)


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


class ExpectedMedia(Model):
    duration: float
    width: int
    height: int
    has_audio: bool


class Check(Model):
    name: str
    passed: bool
    expected: str
    actual: str


class ValidationReport(Model):
    checks: list[Check]
    coverage: Literal["full-decode"] = "full-decode"
    warnings: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)
