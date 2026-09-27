"""Explicit local configuration. Secrets are never included in diagnostic output."""

from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CANVA_SCOPES = [
    "profile:read",
    "asset:read",
    "asset:write",
    "design:meta:read",
    "design:content:read",
    "design:content:write",
]


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
    #: Longest a job-producing tool blocks when a caller passes ``wait_seconds``.
    max_wait_seconds: float = Field(default=120.0, ge=0, le=600)

    # ---------------------------------------------------------------- providers
    canva_enabled: bool = False
    canva_client_id: str | None = None
    canva_client_secret: str | None = None
    canva_redirect_uri: str = "http://127.0.0.1:8765/canva/callback"
    canva_api_base: str = "https://api.canva.com/rest/v1"
    canva_auth_base: str = "https://www.canva.com/api/oauth/authorize"
    canva_token_base: str = "https://api.canva.com/rest/v1/oauth/token"
    canva_scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_CANVA_SCOPES))
    #: Optional Fernet key. When absent a key file is generated under ``data_dir``.
    secret_key: str | None = None
    provider_timeout_seconds: int = Field(default=60, gt=0)
    max_provider_response_bytes: int = Field(default=100_000_000, gt=0)
    provider_poll_seconds: float = Field(default=1.0, gt=0, le=30)
    provider_poll_attempts: int = Field(default=60, ge=1, le=600)

    @model_validator(mode="after")
    def paths(self) -> "Settings":
        self.data_dir = self.data_dir.expanduser().resolve()
        self.allowed_local_roots = [p.expanduser().resolve() for p in self.allowed_local_roots]
        if self.transcription_model_dir:
            self.transcription_model_dir = self.transcription_model_dir.expanduser().resolve()
        if self.canva_enabled and not (self.canva_client_id and self.canva_client_secret):
            raise ValueError(
                "OVE_CANVA_ENABLED requires OVE_CANVA_CLIENT_ID and OVE_CANVA_CLIENT_SECRET."
            )
        return self

    def transcription_config(self) -> dict[str, Any]:
        """The subset of settings the isolated ASR subprocess may receive."""
        return {
            "data_dir": str(self.data_dir),
            "transcription_provider": self.transcription_provider,
            "transcription_model_dir": (
                str(self.transcription_model_dir) if self.transcription_model_dir else None
            ),
            "transcription_device": self.transcription_device,
            "transcription_compute_type": self.transcription_compute_type,
        }

    def redacted(self) -> dict[str, Any]:
        """Diagnostic view. Secret values are replaced, never truncated."""
        body = self.model_dump(mode="json")
        for key in ("canva_client_secret", "secret_key"):
            if body.get(key):
                body[key] = "***"
        body["canva_client_id"] = "***" if self.canva_client_id else None
        return body
