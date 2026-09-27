"""Typed input and output contracts for every MCP tool.

Inputs are strict: unknown fields are rejected so a model cannot smuggle an
unsupported parameter. Outputs mirror exactly what the application layer returns,
so a mismatch is a contract bug rather than a silent surprise for a client.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ove.mcp.envelope import ErrorBody


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


# ----------------------------------------------------------------------------
# Shared output blocks
# ----------------------------------------------------------------------------


class ArtifactSummary(Model):
    artifact_id: str = Field(description="Stable identifier for the produced file.")
    kind: Literal["video", "audio", "image"]
    bytes: int
    sha256: str
    extension: str | None = None
    local_path: str = Field(description="Absolute path to the validated local file.")
    validation: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)


class JobHandle(Model):
    """A queued unit of work. ``succeeded`` alone means a validated file exists."""

    job_id: str
    state: str = Field(description="queued, running, validating, succeeded, failed or cancelled.")
    stage: str
    plan_id: str
    plan_hash: str
    expected: dict[str, Any] = Field(description="The frozen promise the output must satisfy.")
    warnings: list[str] = Field(default_factory=list)
    progress_percent: int | None = Field(
        default=None, description="Coarse value; null while work is in progress."
    )
    artifacts: list[ArtifactSummary] = Field(default_factory=list)
    error: ErrorBody | None = None


class JobStatus(Model):
    job_id: str
    kind: str
    state: str
    stage: str
    progress_percent: int | None = None
    progress_note: str
    created_at: str
    updated_at: str
    payload: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    error: ErrorBody | None = None
    artifacts: list[ArtifactSummary] = Field(default_factory=list)


class TranscriptionJob(Model):
    job_id: str
    state: str
    stage: str
    transcript_id: str | None = None
    error: ErrorBody | None = None
    next_step: str = Field(
        default="Poll get_job_status, then pass transcript_id to generate_captions."
    )


class MediaInfoOut(Model):
    source: str = Field(description="The asset id, or 'path' when a filesystem path was probed.")
    kind: Literal["video", "audio", "image"]
    media: dict[str, Any]
    local_path: str | None = None


class AssetOut(Model):
    asset_id: str
    kind: Literal["video", "audio", "image"]
    bytes: int
    sha256: str
    origin: str
    created_at: str
    media: dict[str, Any]
    local_path: str


class ProjectOut(Model):
    project_id: str
    name: str
    revision: int
    asset_ids: list[str]
    created_at: str


class ProjectSummary(Model):
    project_id: str
    name: str
    revision: int
    asset_count: int
    created_at: str


class ProjectListOut(Model):
    projects: list[ProjectSummary]
    limit: int
    offset: int


class AssetSummary(Model):
    asset_id: str
    kind: str
    bytes: int
    created_at: str
    duration: float | None = None
    width: int | None = None
    height: int | None = None


class AssetListOut(Model):
    assets: list[AssetSummary]
    limit: int
    offset: int


# ----------------------------------------------------------------------------
# Formats
# ----------------------------------------------------------------------------


class FormatOut(Model):
    id: str
    version: int
    status: str
    category: str
    description: str
    settings: dict[str, Any]


class FormatListOut(Model):
    formats: list[FormatOut]
    engine: dict[str, Any] = Field(description="Installed operations, job kinds and encoders.")
    notes: list[str] = Field(default_factory=list)


class FormatCheck(Model):
    name: str
    passed: bool
    detail: str


class FormatValidationOut(Model):
    export: dict[str, Any]
    provenance: dict[str, str]
    compatible: bool
    checks: list[FormatCheck]
    warnings: list[str]


class ExportOut(JobHandle):
    exported_to: str | None = Field(
        default=None, description="Absolute path of the copied file, when one was requested."
    )


# ----------------------------------------------------------------------------
# Captions and text
# ----------------------------------------------------------------------------


class CaptionCuesOut(Model):
    cues: list[dict[str, Any]]
    count: int
    style: str | None = None
    format: str | None = None
    text: str | None = Field(default=None, description="Serialized sidecar when requested.")
    notes: list[str] = Field(default_factory=list)


class TranslationOut(Model):
    target_language: str
    cues: list[dict[str, Any]]
    provider: str | None = None
    word_timing_preserved: bool = False
    note: str


class TextSuggestionsOut(Model):
    kind: Literal["title", "hook", "cta"]
    suggestions: list[str]
    source: Literal["template"] = "template"
    model_generated: Literal[False] = False
    notes: list[str] = Field(default_factory=list)


# ----------------------------------------------------------------------------
# System and jobs
# ----------------------------------------------------------------------------


class CapabilitiesOut(Model):
    schema_version: str
    mode: str
    media: dict[str, Any] = Field(description="Installed engine, operations, jobs and encoders.")
    canva: dict[str, Any]
    transcription: dict[str, Any]
    translation: dict[str, Any]
    unsupported: list[str] = Field(description="Capabilities this build deliberately omits.")
    notes: list[str] = Field(default_factory=list)


class JobSummary(Model):
    job_id: str
    kind: str
    state: str
    stage: str
    progress_percent: int | None = None
    created_at: str
    updated_at: str


class JobListOut(Model):
    jobs: list[JobSummary]
    limit: int
    offset: int


# ----------------------------------------------------------------------------
# Canva
# ----------------------------------------------------------------------------


class CanvaConnectOut(Model):
    status: str | None = None
    connected: bool
    configured: bool | None = None
    authorization_url: str | None = None
    state: str | None = None
    scopes: list[str] = Field(default_factory=list)
    expires_at: float | None = None
    expires_in_seconds: int | None = None
    account: dict[str, Any] | None = None
    refreshable: bool | None = None
    credentials_removed: bool | None = None
    next_step: str | None = None
    secrets_exposed: Literal[False] = False


class CanvaDesignOut(Model):
    design_id: str | None = None
    title: str | None = None
    page_count: int | None = None
    design_types: list[str] = Field(default_factory=list)
    created_at: int | None = None
    updated_at: int | None = None
    edit_url: str | None = None
    view_url: str | None = None
    thumbnail_url: str | None = None
    urls_are_temporary: bool | None = None
    note: str | None = None


class CanvaDesignListOut(Model):
    designs: list[CanvaDesignOut]
    continuation: str | None = None


class CanvaUploadOut(Model):
    asset_id: str | None = None
    name: str | None = None
    local_name: str | None = None
    bytes: int | None = None
    job_id: str | None = None
    verified: bool = False
    inserted_into_design: Literal[False] = False


class CanvaExportOut(Model):
    export_id: str | None = None
    format: str
    file_count: int
    assets: list[dict[str, Any]] = Field(default_factory=list)
    note: str | None = None


# ----------------------------------------------------------------------------
# Input helpers
# ----------------------------------------------------------------------------


class CanvaDesignTypeInput(Model):
    type: Literal["preset", "custom"]
    name: Literal["doc", "email", "presentation", "whiteboard"] | None = None
    width: int | None = Field(default=None, ge=40, le=8000)
    height: int | None = Field(default=None, ge=40, le=8000)

    @model_validator(mode="after")
    def complete(self) -> "CanvaDesignTypeInput":
        if self.type == "preset":
            if self.name is None:
                raise ValueError("a preset design_type requires a name")
        else:
            if self.width is None or self.height is None:
                raise ValueError("a custom design_type requires width and height")
            if self.width * self.height > 25_000_000:
                raise ValueError("Canva limits custom designs to 25,000,000 square pixels")
        return self
