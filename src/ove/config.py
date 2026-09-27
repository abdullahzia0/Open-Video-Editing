"""Explicit local configuration. Secrets are never included in diagnostic output."""

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OVE_", env_file=".env", extra="forbid")

    data_dir: Path = Path(".ove")
    allowed_local_roots: list[Path] = Field(default_factory=list)
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    max_upload_bytes: int = Field(default=2_000_000_000, gt=0)
    max_duration_seconds: int = Field(default=14_400, gt=0)
    max_pixels: int = Field(default=33_177_600, gt=0)
    job_timeout_seconds: int = Field(default=3600, gt=0)
    probe_timeout_seconds: int = Field(default=30, gt=0)
    max_output_bytes: int = Field(default=4_000_000_000, gt=0)
    ffmpeg_threads: int = Field(default=2, ge=1, le=64)
    offline: bool = True
    transcription_provider: Literal["disabled", "faster-whisper"] = "disabled"
    transcription_model_dir: Path | None = None
    transcription_device: Literal["cpu", "cuda"] = "cpu"
    transcription_compute_type: str = "int8"
    preset_dir: Path | None = None

    @model_validator(mode="after")
    def paths(self) -> "Settings":
        self.data_dir = self.data_dir.expanduser().resolve()
        self.allowed_local_roots = [p.expanduser().resolve() for p in self.allowed_local_roots]
        if self.transcription_model_dir:
            self.transcription_model_dir = self.transcription_model_dir.expanduser().resolve()
        return self
