"""Application use cases; callable from CLI, MCP or another trusted local client.

This module holds no transport concerns and no vendor SDK types. Every mutating
operation either queues a durable job or writes an immutable record; nothing here
reports an edit as complete before the worker has validated its output.
"""

import hashlib
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from ove.application.planning import export_effects, validate_media_job, validate_plan
from ove.captions.subtitles import serialize
from ove.captions.text import suggestions
from ove.config import Settings
from ove.domain.errors import OveError
from ove.domain.models import (
    CaptionCue,
    CaptionStyle,
    Captions,
    Effect,
    ExportSpec,
    Intent,
    MediaInfo,
    MediaJobRequest,
    PlanRequest,
    ProbedMedia,
    Resize,
    TextOverlay,
    Trim,
)
from ove.formats.registry import PresetRegistry
from ove.motion.ass import CAPTION_STYLES
from ove.ports.media import MediaEngine
from ove.ports.providers import DesignProvider, TranscriptionProvider
from ove.ports.repository import Repository
from ove.ports.storage import BlobStore
from ove.security.files import open_allowed_file
from ove.utilities.identity import digest, new_id, now

TERMINAL_STATES = {"succeeded", "failed", "validation_failed", "cancelled"}
STAGE_FOR_STATE = {
    "queued": "queued",
    "running": "processing",
    "validating": "validating",
    "succeeded": "completed",
    "failed": "failed",
    "validation_failed": "failed",
    "cancel_requested": "cancelling",
    "cancelled": "cancelled",
}

PROGRESS_FOR_STATE = {"queued": 0, "succeeded": 100}

#: Job kinds the worker understands, mapped from a plan's job specification.
MEDIA_JOB_KINDS = {
    "concat",
    "split",
    "frame",
    "audio_extract",
    "audio_process",
    "audio_replace",
    "audio_mix",
    "overlay",
}


class EditingService:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        storage: BlobStore,
        engine: MediaEngine,
        presets: PresetRegistry,
        design: DesignProvider,
        transcription: TranscriptionProvider,
        translation: Any,
    ):
        self.settings, self.repository, self.storage = settings, repository, storage
        self.engine, self.presets, self.design = engine, presets, design
        self.transcription, self.translation = transcription, translation

    # ------------------------------------------------------------------ capability

    def capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "mode": "local-single-user",
            "media": self.engine.capabilities(),
            "canva": self.design.capabilities(),
            "transcription": self.transcription.capabilities(),
            "translation": self.translation.capabilities(),
            "unsupported": [
                "neural_upscale",
                "generative_editing",
                "headless_canva_element_editing",
                "remote_asset_import",
                "hosted_multi_tenant",
                "social_publishing",
                "file_size_target",
            ],
            "notes": [
                "Preview outputs before publication; automated checks do not measure aesthetics.",
                "A separate local worker must run for any queued job to make progress.",
            ],
        }

    # ---------------------------------------------------------------------- assets

    def _asset(self, asset_id: str) -> dict[str, Any]:
        return self.repository.get("asset", asset_id)

    def _asset_media(self, asset_id: str) -> ProbedMedia:
        return ProbedMedia.model_validate(self._asset(asset_id)["media"])

    def import_asset(self, source: str) -> dict[str, Any]:
        key, checksum, size = self.storage.ingest(source)
        media = self.engine.inspect(self.storage.path(key))
        asset: dict[str, Any] = {
            "id": new_id("asset"),
            "blob": key,
            "sha256": checksum,
            "bytes": size,
            "kind": media.kind,
            "media": media.model_dump(mode="json"),
            "origin": "import",
            "created_at": now(),
        }
        self.repository.put("asset", asset["id"], asset)
        return asset

    def register_file_asset(self, path: Path, name: str, origin: str) -> dict[str, Any]:
        """Publish an externally produced file as a new immutable asset."""
        key, checksum, size = self.storage.publish(path)
        media = self.engine.inspect(self.storage.path(key))
        asset: dict[str, Any] = {
            "id": new_id("asset"),
            "blob": key,
            "sha256": checksum,
            "bytes": size,
            "name": name,
            "kind": media.kind,
            "media": media.model_dump(mode="json"),
            "origin": origin,
            "created_at": now(),
        }
        self.repository.put("asset", asset["id"], asset)
        return asset

    def asset_info(self, asset_id: str) -> dict[str, Any]:
        asset = self._asset(asset_id)
        return {
            "asset_id": asset["id"],
            "kind": asset["kind"],
            "bytes": asset["bytes"],
            "sha256": asset["sha256"],
            "origin": asset.get("origin", "import"),
            "created_at": asset["created_at"],
            "media": asset["media"],
            "local_path": str(self.storage.path(asset["blob"])),
        }

    def media_info(self, source: str, as_path: bool = False) -> dict[str, Any]:
        """Probe a registered asset or an allowlisted path without importing anything."""
        if as_path:
            descriptor = open_allowed_file(source, self.settings.allowed_local_roots)
            os.close(descriptor)
            path = Path(source).expanduser().resolve()
            media = self.engine.inspect(path)
            return {
                "source": "path",
                "kind": media.kind,
                "media": media.model_dump(mode="json"),
                "local_path": str(path),
            }
        asset = self._asset(source)
        return {
            "source": source,
            "kind": asset["kind"],
            "media": asset["media"],
            "local_path": str(self.storage.path(asset["blob"])),
        }

    # -------------------------------------------------------------------- projects

    def create_project(self, name: str, asset_ids: list[str] | None = None) -> dict[str, Any]:
        asset_ids = list(dict.fromkeys(asset_ids or []))
        if not name.strip() or len(name) > 200 or len(asset_ids) > 100:
            raise OveError(
                "invalid_project", "Provide a name of 1-200 characters and up to 100 assets."
            )
        for identifier in asset_ids:
            self._asset(identifier)
        project: dict[str, Any] = {
            "id": new_id("project"),
            "name": name,
            "revision": 1,
            "asset_ids": asset_ids,
            "created_at": now(),
        }
        self.repository.put("project", project["id"], project)
        self.repository.put("revision", f"{project['id']}:1", project)
        return project

    def get_project(self, project_id: str, revision: int | None = None) -> dict[str, Any]:
        if revision is None:
            return self.repository.get("project", project_id)
        return self.repository.get("revision", f"{project_id}:{revision}")

    def update_project(
        self,
        project_id: str,
        expected_revision: int,
        name: str | None = None,
        asset_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        changes: dict[str, Any] = {}
        if name is not None:
            if not name.strip() or len(name) > 200:
                raise OveError("invalid_project", "Provide a name of 1-200 characters.")
            changes["name"] = name
        if asset_ids is not None:
            unique = list(dict.fromkeys(asset_ids))
            if len(unique) > 100:
                raise OveError("invalid_project", "A project holds at most 100 assets.")
            for identifier in unique:
                self._asset(identifier)
            changes["asset_ids"] = unique
        if not changes:
            raise OveError("invalid_project", "Supply a new name, a new asset list, or both.")
        return self.repository.revise_project(project_id, expected_revision, changes)

    def revise_project(
        self, project_id: str, expected: int, asset_ids: list[str]
    ) -> dict[str, Any]:
        return self.update_project(project_id, expected, asset_ids=asset_ids)

    def add_asset(self, project_id: str, expected_revision: int, asset_id: str) -> dict[str, Any]:
        self._asset(asset_id)
        project = self.repository.get("project", project_id)
        if asset_id in project["asset_ids"]:
            raise OveError(
                "invalid_project",
                "This asset already belongs to the project.",
                "Revisions are additive; no change was recorded.",
            )
        return self.update_project(
            project_id, expected_revision, asset_ids=[*project["asset_ids"], asset_id]
        )

    def remove_asset(
        self, project_id: str, expected_revision: int, asset_id: str
    ) -> dict[str, Any]:
        project = self.repository.get("project", project_id)
        if asset_id not in project["asset_ids"]:
            raise OveError("invalid_project", "This asset is not part of the project.")
        remaining = [item for item in project["asset_ids"] if item != asset_id]
        return self.update_project(project_id, expected_revision, asset_ids=remaining)

    def list_projects(self, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        projects = self.repository.list_objects("project", limit, offset)
        return {
            "projects": [
                {
                    "project_id": project["id"],
                    "name": project["name"],
                    "revision": project["revision"],
                    "asset_count": len(project["asset_ids"]),
                    "created_at": project["created_at"],
                }
                for project in projects
            ],
            "limit": limit,
            "offset": offset,
        }

    def list_assets(
        self, project_id: str | None = None, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        if project_id is None:
            assets = self.repository.list_objects("asset", limit, offset)
        else:
            project = self.repository.get("project", project_id)
            assets = [self._asset(identifier) for identifier in project["asset_ids"]]
        return {
            "assets": [
                {
                    "asset_id": asset["id"],
                    "kind": asset.get("kind", "video"),
                    "bytes": asset["bytes"],
                    "created_at": asset["created_at"],
                    "duration": asset["media"]
                    .get("video", asset["media"].get("audio", {}))
                    .get("duration"),
                    "width": (asset["media"].get("video") or asset["media"].get("image") or {}).get(
                        "width"
                    ),
                    "height": (
                        asset["media"].get("video") or asset["media"].get("image") or {}
                    ).get("height"),
                }
                for asset in assets
            ],
            "limit": limit,
            "offset": offset,
        }

    # ----------------------------------------------------------------------- plans

    def _inputs_for(self, asset_ids: list[str]) -> list[dict[str, str]]:
        return [
            {"asset_id": identifier, "sha256": self._asset(identifier)["sha256"]}
            for identifier in asset_ids
        ]

    def _store_plan(
        self,
        kind: str,
        request_body: dict[str, Any],
        expected: Any,
        asset_ids: list[str],
        warnings: list[str],
    ) -> dict[str, Any]:
        inputs = self._inputs_for(asset_ids)
        capabilities = self.engine.capabilities()
        plan: dict[str, Any] = {
            "id": new_id("plan"),
            "kind": kind,
            "request": request_body,
            "expected": expected.model_dump(mode="json"),
            "engine": capabilities,
            "inputs": inputs,
            "warnings": warnings,
            "created_at": now(),
        }
        plan["hash"] = digest({"request": request_body, "inputs": inputs, "engine": capabilities})
        self.repository.put("plan", plan["id"], plan)
        return plan

    def _check_engine(self, request: PlanRequest, capabilities: dict[str, object]) -> None:
        if not capabilities.get("available"):
            raise OveError(
                "missing_dependency",
                "FFmpeg with libx264 is not available.",
                "Install the required engine and inspect the capability resource.",
            )
        supported = capabilities.get("operations", [])
        if not isinstance(supported, list):
            raise OveError("invalid_capabilities", "Engine returned malformed capabilities.")
        missing = {operation.type for operation in request.operations} - set(supported)
        if request.export.width is not None and "resize" not in supported:
            missing.add("resize")
        if missing:
            raise OveError(
                "unsupported_operation",
                f"Unavailable engine operations: {sorted(missing)}",
                "Inspect the capability resource and choose a supported operation.",
            )
        if request.export.video_codec == "copy" and request.operations:
            raise OveError(
                "unsupported_operation",
                "Stream-copy output cannot carry filters or edits.",
                "Set video_codec to h264, hevc or vp9, or remove the operations.",
            )
        needs_aac = request.export.audio == "aac" or any(
            operation.type in {"trim", "speed", "cut", "freeze"} for operation in request.operations
        )
        encoders = capabilities.get("encoders", [])
        if needs_aac and (not isinstance(encoders, list) or "aac" not in encoders):
            raise OveError("missing_dependency", "The selected engine has no AAC encoder.")

    def create_plan(self, request: PlanRequest) -> dict[str, Any]:
        if request.project_id is not None:
            project = self.repository.get("project", request.project_id)
            if request.revision != project["revision"]:
                raise OveError(
                    "revision_conflict", "Fetch the current project revision before planning."
                )
            if request.asset_id not in project["asset_ids"]:
                raise OveError("invalid_asset", "Asset is not part of this project revision.")
        asset = self._asset(request.asset_id)
        media = ProbedMedia.model_validate(asset["media"])
        if media.video is None:
            raise OveError(
                "no_video",
                "This operation requires a video asset.",
                "Use the audio or frame tools for audio-only and image assets.",
            )
        info = media.video
        expected = validate_plan(request, info, self.settings.max_pixels)
        capabilities = self.engine.capabilities()
        self._check_engine(request, capabilities)
        body = request.model_dump(mode="json")
        # Sort a set explicitly; canonical hashes must survive process hash randomization.
        body["intent"]["allowed_effects"] = sorted(request.intent.allowed_effects)
        warnings = self._render_warnings(request, info)
        return self._store_plan("render", body, expected, [request.asset_id], warnings)

    def _render_warnings(self, request: PlanRequest, info: MediaInfo) -> list[str]:
        warnings = ["Video is re-encoded; source blobs remain unchanged."]
        temporal = {"trim", "speed", "cut", "freeze"}
        if any(operation.type in temporal for operation in request.operations) and info.audio_codec:
            warnings.append("Temporal edits re-encode audio to AAC to preserve edited timing.")
        if any(operation.type == "speed" for operation in request.operations):
            warnings.append(
                "Speed changes resample video to a constant frame rate of source rate x factor; "
                "variable-frame-rate timestamps are not preserved."
            )
        if any(
            operation.type in {"title", "text_overlay", "captions"}
            for operation in request.operations
        ):
            warnings.append(
                "Text uses installed font fallback; visually review glyphs and line wrapping."
            )
        if any(operation.type == "stabilize" for operation in request.operations):
            warnings.append(
                "Stabilization reframes the shot and can introduce black borders or a zoom crop."
            )
        if any(operation.type == "upscale" for operation in request.operations):
            warnings.append(
                "Upscaling resamples existing pixels; it does not recover uncaptured detail."
            )
        if any(operation.type == "zoom" for operation in request.operations):
            warnings.append("Zoom animations crop the frame; confirm the subject stays in view.")
        return warnings

    def create_media_plan(self, request: MediaJobRequest) -> dict[str, Any]:
        if request.project_id is not None:
            project = self.repository.get("project", request.project_id)
            if request.revision != project["revision"]:
                raise OveError(
                    "revision_conflict", "Fetch the current project revision before planning."
                )
            unknown = set(request.asset_ids) - set(project["asset_ids"])
            if unknown:
                raise OveError("invalid_asset", "An asset is not part of this project revision.")
        media = [self._asset_media(identifier) for identifier in request.asset_ids]
        expected = validate_media_job(request, media, self.settings.max_pixels)
        self.engine.require_job(request.job.type)
        if request.job.type == "concat" and request.job.transition != "none":
            self.engine.require_job("concat_transition")
        body = request.model_dump(mode="json")
        body["intent"]["allowed_effects"] = sorted(request.intent.allowed_effects)
        return self._store_plan(
            request.job.type, body, expected, request.asset_ids, self._job_warnings(request)
        )

    @staticmethod
    def _job_warnings(request: MediaJobRequest) -> list[str]:
        warnings = ["Video is re-encoded; source blobs remain unchanged."]
        if request.job.type == "concat":
            warnings.append(
                "Merged clips are normalized to one canvas and frame rate; timing follows the "
                "selected transition policy."
            )
        if request.job.type == "split":
            warnings.append("Each segment is encoded independently from the same source.")
        if request.job.type == "audio_process":
            warnings.append(
                "Loudness normalization changes gain; confirm it against your delivery spec."
            )
        if request.job.type == "overlay":
            warnings.append(
                "The overlay is composited over the base video; the overlay asset is not modified."
            )
        return warnings

    def validate_plan(self, plan_id: str, plan_hash: str) -> dict[str, Any]:
        plan = self.repository.get("plan", plan_id)
        if plan["hash"] != plan_hash:
            raise OveError("plan_mismatch", "Plan hash does not match the immutable plan.")
        for entry in plan["inputs"]:
            asset = self._asset(entry["asset_id"])
            with self.storage.path(asset["blob"]).open("rb") as source:
                actual = hashlib.file_digest(source, "sha256").hexdigest()
            if actual != entry["sha256"]:
                raise OveError(
                    "source_changed", "Stored source no longer matches its plan checksum."
                )
        capabilities = self.engine.capabilities()
        if capabilities != plan["engine"]:
            raise OveError(
                "engine_changed",
                "Engine capabilities changed after planning.",
                "Create a new plan to capture the new engine fingerprint.",
            )
        if plan["kind"] == "render":
            render_request = PlanRequest.model_validate(plan["request"])
            media = self._asset_media(render_request.asset_id)
            assert media.video is not None
            expected = validate_plan(render_request, media.video, self.settings.max_pixels)
            self._check_engine(render_request, capabilities)
        else:
            job_request = MediaJobRequest.model_validate(plan["request"])
            media_list = [self._asset_media(identifier) for identifier in job_request.asset_ids]
            expected = validate_media_job(job_request, media_list, self.settings.max_pixels)
            self.engine.require_job(job_request.job.type)
        return {
            "ready": True,
            "plan_id": plan_id,
            "hash": plan_hash,
            "kind": plan["kind"],
            "expected": expected.model_dump(mode="json"),
            "warnings": plan["warnings"],
        }

    def submit_plan(
        self, plan_id: str, plan_hash: str, key: str, wait_seconds: float = 0.0
    ) -> dict[str, Any]:
        self.validate_plan(plan_id, plan_hash)
        plan = self.repository.get("plan", plan_id)
        job = self.repository.submit(
            plan["kind"], {"plan_id": plan_id, "hash": plan_hash}, key, plan_hash
        )
        if wait_seconds > 0:
            self._await(job["id"], wait_seconds)
        return self.job_status(job["id"])

    #: Backwards-compatible name used by the CLI and the documented example.
    def submit_render(self, plan_id: str, plan_hash: str, key: str) -> dict[str, Any]:
        return self.submit_plan(plan_id, plan_hash, key)

    def plan_and_submit(
        self,
        request: PlanRequest | MediaJobRequest,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        """Plan, freeze and queue one intent-level request in a single step."""
        plan = (
            self.create_plan(request)
            if isinstance(request, PlanRequest)
            else self.create_media_plan(request)
        )
        key = idempotency_key or f"auto-{digest(request.model_dump(mode='json'))[:40]}"
        result = self.submit_plan(plan["id"], plan["hash"], key, wait_seconds)
        return {
            "job_id": result["job_id"],
            "state": result["state"],
            "stage": result["stage"],
            "plan_id": plan["id"],
            "plan_hash": plan["hash"],
            "expected": plan["expected"],
            "warnings": plan["warnings"],
            "progress_percent": result["progress_percent"],
            "artifacts": result["artifacts"],
            "error": result["error"],
        }

    def _await(self, job_id: str, seconds: float) -> None:
        from ove.application.worker import Worker

        deadline = time.monotonic() + min(seconds, self.settings.max_wait_seconds)
        while time.monotonic() < deadline:
            if self.repository.job(job_id)["state"] in TERMINAL_STATES:
                return
            try:
                Worker(self).run(once=True)
            except OveError as exc:
                if exc.code != "CONFLICT":
                    raise
            time.sleep(0.15)

    # ------------------------------------------------------------------------ jobs

    def _artifact_summary(self, artifact_id: str) -> dict[str, Any]:
        artifact = self.repository.get("artifact", artifact_id)
        return {
            "artifact_id": artifact["id"],
            "kind": artifact.get("kind", "video"),
            "bytes": artifact["bytes"],
            "sha256": artifact["sha256"],
            "extension": artifact.get("extension"),
            "local_path": str(self.storage.path(artifact["blob"])),
            "validation": artifact.get("validation"),
            "warnings": artifact.get("warnings", []),
        }

    def job_status(self, job_id: str) -> dict[str, Any]:
        job = self.repository.job(job_id)
        state = job["state"]
        artifact_ids: list[str] = []
        if job["result"]:
            if job["result"].get("artifact_ids"):
                artifact_ids = list(job["result"]["artifact_ids"])
            elif job["result"].get("artifact_id"):
                artifact_ids = [job["result"]["artifact_id"]]
        return {
            "job_id": job["id"],
            "kind": job["kind"],
            "state": state,
            "stage": STAGE_FOR_STATE.get(state, state),
            "progress_percent": PROGRESS_FOR_STATE.get(state),
            "progress_note": "The local engine reports stage transitions, not measured completion.",
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "payload": job["payload"],
            "error": job["error"],
            "artifacts": [self._artifact_summary(item) for item in artifact_ids],
        }

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        self.repository.cancel(job_id)
        return self.job_status(job_id)

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        artifact = self.repository.get("artifact", artifact_id)
        job = self.repository.job(artifact["job_id"])
        if job["state"] != "succeeded":
            raise OveError("artifact_not_ready", "The producing job has not succeeded.")
        return {**artifact, "local_path": str(self.storage.path(artifact["blob"]))}

    def artifacts_for_job(self, job_id: str) -> list[dict[str, Any]]:
        return self.repository.find_objects("artifact", "job_id", job_id, limit=200)

    def retry(self, job_id: str, key: str) -> dict[str, Any]:
        """Queue a fresh attempt at failed or cancelled work under a new key."""
        job = self.repository.job(job_id)
        if key == job["idempotency_key"]:
            raise OveError("idempotency_conflict", "A retry requires a new idempotency key.")
        if job["state"] not in {"failed", "validation_failed", "cancelled"}:
            raise OveError("not_retryable", "Only failed or cancelled jobs can be retried.")
        if job["kind"] == "transcribe":
            created = self.submit_transcript(
                job["payload"]["asset_id"], job["payload"]["language"], key
            )
            return self.job_status(created["id"])
        if job["kind"] in {"render", *MEDIA_JOB_KINDS}:
            return self.submit_plan(job["payload"]["plan_id"], job["payload"]["hash"], key)
        raise OveError("unsupported_operation", "This job kind cannot be retried.")

    def list_jobs(
        self, state: str | None = None, limit: int = 20, offset: int = 0
    ) -> dict[str, Any]:
        """Summarize queued and historical jobs, most recent first."""
        if limit < 1 or limit > 200 or offset < 0:
            raise OveError("invalid_input", "Use a limit of 1-200 and a non-negative offset.")
        jobs = self.repository.list_objects("job", limit, offset)
        if state is not None:
            jobs = [job for job in jobs if job["state"] == state]
        return {
            "jobs": [
                {
                    "job_id": job["id"],
                    "kind": job["kind"],
                    "state": job["state"],
                    "stage": STAGE_FOR_STATE.get(job["state"], job["state"]),
                    "progress_percent": PROGRESS_FOR_STATE.get(job["state"]),
                    "created_at": job["created_at"],
                    "updated_at": job["updated_at"],
                }
                for job in jobs
            ],
            "limit": limit,
            "offset": offset,
        }

    # --------------------------------------------------------------------- formats

    def _preset(self, format_id: str) -> dict[str, Any]:
        if format_id not in self.presets.presets:
            raise OveError(
                "unknown_preset",
                f"Unknown format preset: {format_id}",
                "Call list_formats to see the installed identifiers.",
            )
        return self.presets.presets[format_id]

    def list_formats(self, category: str | None = None) -> dict[str, Any]:
        catalog = self.presets.catalog()
        if category:
            catalog = [item for item in catalog if item.get("category") == category]
        return {
            "formats": catalog,
            "engine": self.engine.capabilities(),
            "notes": [
                "Platform presets are advisory editing defaults, not verified upload limits.",
                "Resolve a format with apply_format or export_media to render it.",
            ],
        }

    def get_format(self, format_id: str) -> dict[str, Any]:
        return self._preset(format_id)

    def resolve_format(
        self, format_ids: list[str], overrides: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return self.presets.resolve(format_ids, overrides)

    def validate_format(
        self,
        format_ids: list[str],
        overrides: dict[str, Any] | None = None,
        asset_id: str | None = None,
    ) -> dict[str, Any]:
        resolved = self.presets.resolve(format_ids, overrides)
        export = ExportSpec.model_validate(resolved["export"])
        warnings = list(resolved["warnings"])
        checks: list[dict[str, Any]] = []
        if asset_id is not None:
            media = self._asset_media(asset_id)
            if media.video is not None and export.width and export.height:
                checks.append(
                    {
                        "name": "aspect_change",
                        "passed": abs(
                            (export.width / export.height)
                            - (media.video.width / media.video.height)
                        )
                        < 0.01,
                        "detail": "The export canvas differs from the source aspect ratio.",
                    }
                )
            if media.video is not None and media.video.duration:
                warnings.append(
                    f"Source duration is {media.video.duration:.2f}s; confirm the target surface "
                    "accepts that length."
                )
            if media.kind == "image":
                warnings.append("This asset is a still image; a video format will loop one frame.")
        capabilities = self.engine.capabilities()
        encoder = {
            "h264": "libx264",
            "hevc": "libx265",
            "vp9": "libvpx-vp9",
            "copy": None,
        }[export.video_codec]
        encoders = capabilities.get("encoders", [])
        compatible = encoder is None or (isinstance(encoders, list) and encoder in encoders)
        if not compatible:
            warnings.append(f"This engine cannot encode {export.video_codec}.")
        return {
            "export": export.model_dump(mode="json"),
            "provenance": resolved["provenance"],
            "compatible": compatible,
            "checks": checks,
            "warnings": warnings,
        }

    def _format_request(
        self, asset_id: str, format_ids: list[str], overrides: dict[str, Any] | None
    ) -> PlanRequest:
        resolved = self.presets.resolve(format_ids, overrides)
        export = ExportSpec.model_validate(resolved["export"])
        media = self._asset_media(asset_id)
        if media.video is None:
            raise OveError(
                "no_video",
                "Format rendering requires a video asset.",
                "Use audio_extract for audio-only assets.",
            )
        effects = export_effects(export)
        operations: list[Any] = []
        if media.video.width != export.width or media.video.height != export.height:
            if export.width and export.height:
                operations.append(Resize(width=export.width, height=export.height, fit=export.fit))
        return PlanRequest(
            project_id=None,
            revision=None,
            asset_id=asset_id,
            operations=operations,
            intent=Intent(
                request=f"Render asset {asset_id} with format {', '.join(format_ids)}",
                allowed_effects=effects,
            ),
            export=export,
        )

    def apply_format(
        self,
        asset_id: str,
        format_ids: list[str],
        overrides: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        return self.plan_and_submit(
            self._format_request(asset_id, format_ids, overrides), idempotency_key, wait_seconds
        )

    def export_media(
        self,
        asset_id: str,
        format_ids: list[str],
        overrides: dict[str, Any] | None = None,
        output_path: str | None = None,
        idempotency_key: str | None = None,
        wait_seconds: float = 90.0,
    ) -> dict[str, Any]:
        result = self.plan_and_submit(
            self._format_request(asset_id, format_ids, overrides), idempotency_key, wait_seconds
        )
        if output_path is None:
            return result
        if result["state"] != "succeeded" or not result["artifacts"]:
            raise OveError(
                "export_failed",
                f"The export did not complete; job state is '{result['state']}'.",
                "Inspect the job error, then retry or shorten the wait budget.",
                retryable=True,
            )
        destination = Path(output_path).expanduser()
        if destination.exists():
            raise OveError(
                "invalid_input", "The output path already exists; nothing was overwritten."
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        source = Path(result["artifacts"][0]["local_path"])
        with destination.open("xb") as target, source.open("rb") as handle:
            shutil.copyfileobj(handle, target)
        return {**result, "exported_to": str(destination.resolve())}

    # -------------------------------------------------------------------- captions

    def transcript(self, transcript_id: str) -> dict[str, Any]:
        transcript = self.repository.get("transcript", transcript_id)
        if self.repository.job(transcript["job_id"])["state"] != "succeeded":
            raise OveError("transcript_not_ready", "The transcription job has not succeeded.")
        return transcript

    def submit_transcript(self, asset_id: str, language: str | None, key: str) -> dict[str, Any]:
        asset = self._asset(asset_id)
        media = ProbedMedia.model_validate(asset["media"])
        if not media.has_audio:
            raise OveError("no_audio", "This asset has no audio stream.")
        if not self.transcription.capabilities().get("available"):
            raise OveError(
                "provider_unavailable",
                "Local transcription is not configured.",
                "Install the transcription extra and configure local model files.",
            )
        payload = {"asset_id": asset_id, "language": language}
        return self.repository.submit("transcribe", payload, key, digest(payload))

    def transcribe_request(
        self, asset_id: str, language: str | None, key: str | None
    ) -> dict[str, Any]:
        """Queue transcription and report a normalized handle for the client."""
        created = self.submit_transcript(
            asset_id,
            language,
            key or f"auto-{digest({'asset_id': asset_id, 'language': language})[:40]}",
        )
        record = self.repository.job(created["id"])
        state = record["state"]
        result = record["result"] or {}
        return {
            "job_id": record["id"],
            "state": state,
            "stage": STAGE_FOR_STATE.get(state, state),
            "transcript_id": result.get("transcript_id"),
            "error": record["error"],
        }

    @staticmethod
    def cues_from_text(
        text: str,
        duration: float,
        start: float = 0.0,
        max_characters: int = 84,
        max_duration: float = 6.0,
    ) -> list[CaptionCue]:
        """Split supplied text into readable, non-overlapping cues across the timeline."""
        words = text.split()
        if not words:
            raise OveError("invalid_input", "The caption text is empty.")
        if duration <= 0:
            raise OveError("invalid_input", "A positive duration is required to place captions.")
        if max_characters < 16 or max_duration <= 0.5:
            raise OveError("invalid_input", "Caption limits are too small to be readable.")
        chunks: list[str] = []
        current: list[str] = []
        for word in words:
            candidate = " ".join([*current, word])
            if current and len(candidate) > max_characters:
                chunks.append(" ".join(current))
                current = [word]
            else:
                current.append(word)
        if current:
            chunks.append(" ".join(current))
        span = duration - start
        per_chunk = min(max_duration, span / len(chunks))
        if per_chunk <= 0.2:
            raise OveError(
                "invalid_input",
                "The text cannot be spread across the requested duration legibly.",
                "Shorten the text, lengthen the range, or raise max_characters.",
            )
        cues: list[CaptionCue] = []
        cursor = start
        for index, chunk in enumerate(chunks):
            end = duration if index == len(chunks) - 1 else min(duration, cursor + per_chunk)
            cues.append(CaptionCue(start=round(cursor, 3), end=round(end, 3), text=chunk))
            cursor = end
        return cues

    def cues_from_transcript(
        self, transcript_id: str, max_characters: int = 84, max_duration: float = 6.0
    ) -> list[CaptionCue]:
        transcript = self.transcript(transcript_id)
        cues: list[CaptionCue] = []
        for segment in transcript.get("segments", []):
            text = " ".join(str(segment.get("text", "")).split())
            if not text:
                continue
            start = float(segment["start"])
            end = float(segment["end"])
            if end - start > max_duration or len(text) > max_characters:
                cues.extend(
                    self.cues_from_text(
                        text, end, start, max_characters=max_characters, max_duration=max_duration
                    )
                )
            elif cues and start < cues[-1].end:
                cues.append(
                    CaptionCue(start=cues[-1].end, end=max(end, cues[-1].end + 0.2), text=text)
                )
            else:
                cues.append(CaptionCue(start=start, end=end, text=text))
        if not cues:
            raise OveError("invalid_input", "The transcript contains no usable speech segments.")
        return cues

    def restyle_captions(
        self,
        cues: list[CaptionCue],
        style: str,
        max_characters: int = 84,
        max_duration: float = 6.0,
    ) -> list[CaptionCue]:
        """Validate a caption style and re-split cues that exceed readability limits."""
        if style not in CAPTION_STYLES:
            raise OveError(
                "invalid_input",
                f"Unknown caption style: {style}",
                f"Choose one of: {', '.join(sorted(CAPTION_STYLES))}.",
            )
        if max_characters < 16 or max_duration <= 0.5:
            raise OveError("invalid_input", "Caption limits are too small to be readable.")
        styled: list[CaptionCue] = []
        for cue in cues:
            text = " ".join(cue.text.split())
            if not text:
                continue
            if len(text) > max_characters or (cue.end - cue.start) > max_duration:
                styled.extend(
                    self.cues_from_text(
                        text,
                        cue.end,
                        cue.start,
                        max_characters=max_characters,
                        max_duration=max_duration,
                    )
                )
            else:
                styled.append(cue)
        if not styled:
            raise OveError("invalid_input", "No usable caption cues were supplied.")
        return styled

    @staticmethod
    def caption_sidecar(cues: list[CaptionCue], format_name: str) -> str:
        return serialize(cues, format_name)

    @staticmethod
    def text_suggestions(
        kind: str,
        subject: str,
        detail: str | None = None,
        audience: str | None = None,
        limit: int = 5,
    ) -> list[str]:
        """Compose template-based copy. No language model is invoked."""
        return suggestions(kind, subject, detail, audience, limit)  # type: ignore[arg-type]

    def translate_captions(self, cues: list[CaptionCue], target_language: str) -> dict[str, Any]:
        payload = [cue.model_dump(mode="json") for cue in cues]
        result = self.translation.translate(payload, target_language)
        return {
            "target_language": target_language,
            "cues": result.get("segments", []),
            "provider": result.get("provider"),
            "word_timing_preserved": False,
            "note": "Translated text keeps segment timing; word-level timing is not transferred.",
        }

    def burn_captions_request(
        self,
        asset_id: str,
        cues: list[CaptionCue],
        style: CaptionStyle,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        media = self._asset_media(asset_id)
        if media.video is None:
            raise OveError("no_video", "Burning captions requires a video asset.")
        effects: set[Effect] = {"text"}
        request = PlanRequest(
            asset_id=asset_id,
            operations=[Captions(cues=cues, style=style)],
            intent=Intent(
                request=f"Burn {len(cues)} caption cues into asset {asset_id}",
                allowed_effects=effects,
            ),
        )
        return self.plan_and_submit(request, idempotency_key, wait_seconds)

    def text_overlay_request(
        self,
        asset_id: str,
        overlay: TextOverlay,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        text_effects: set[Effect] = {"text"}
        request = PlanRequest(
            asset_id=asset_id,
            operations=[overlay],
            intent=Intent(
                request=f"Add a {overlay.template} overlay to asset {asset_id}",
                allowed_effects=text_effects,
            ),
        )
        return self.plan_and_submit(request, idempotency_key, wait_seconds)

    def create_preview(
        self,
        asset_id: str,
        max_height: int = 480,
        start: float | None = None,
        end: float | None = None,
        idempotency_key: str | None = None,
        wait_seconds: float = 0.0,
    ) -> dict[str, Any]:
        media = self._asset_media(asset_id)
        if media.video is None:
            raise OveError("no_video", "A preview requires a video asset.")
        video = media.video
        height = min(max_height, video.height)
        width = max(2, round(video.width * height / video.height) // 2 * 2)
        export = ExportSpec(crf=30, encoder_preset="ultrafast", audio="aac")
        operations: list[Any] = []
        effects = {"geometry"} | export_effects(export)
        if start is not None or end is not None:
            if start is None or end is None:
                raise OveError("invalid_input", "Supply both start and end, or neither.")
            if end <= start or end > video.duration + 0.001:
                raise OveError("invalid_range", "The preview range is outside the source.")
            operations.append(Trim(start=start, end=end))
            effects.add("timing")
        operations.append(Resize(width=width, height=height, fit="pad"))
        request = PlanRequest(
            asset_id=asset_id,
            operations=operations,
            intent=Intent(
                request=f"Render a {width}x{height} review preview of asset {asset_id}",
                allowed_effects=effects,
            ),
            export=export,
        )
        return self.plan_and_submit(request, idempotency_key, wait_seconds)

    # ----------------------------------------------------------------------- canva

    def canva_connect(
        self, action: str, code: str | None = None, state: str | None = None
    ) -> dict[str, Any]:
        return self.design.connect(action, code, state)

    def canva_list_designs(
        self, query: str | None = None, limit: int = 20, continuation: str | None = None
    ) -> dict[str, Any]:
        return self.design.list_designs(query, limit, continuation)

    def canva_get_design(self, design_id: str) -> dict[str, Any]:
        return self.design.get_design(design_id)

    def canva_create_design(
        self,
        design_type: dict[str, Any] | None = None,
        title: str | None = None,
        asset_id: str | None = None,
        copy_design_id: str | None = None,
        brand_template_id: str | None = None,
    ) -> dict[str, Any]:
        if not any((design_type, asset_id, copy_design_id, brand_template_id)):
            raise OveError(
                "invalid_input",
                "Supply a design_type, an asset_id, a copy_design_id or a brand_template_id.",
            )
        mode = (
            "design"
            if copy_design_id
            else "brand_template"
            if brand_template_id
            else "type_and_asset"
        )
        return self.design.create_design(
            {
                "type": mode,
                "design_type": design_type,
                "title": title,
                "asset_id": asset_id,
                "design_id": copy_design_id,
                "brand_template_id": brand_template_id,
            }
        )

    def canva_edit_design(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.design.edit_design(request)

    def canva_upload_asset(
        self, asset_id: str | None = None, path: str | None = None, name: str | None = None
    ) -> dict[str, Any]:
        if asset_id is not None:
            asset = self._asset(asset_id)
            local = self.storage.path(asset["blob"])
            label = name or asset.get("name") or f"{asset_id}.{Path(asset['blob']).suffix or 'bin'}"
        elif path is not None:
            descriptor = open_allowed_file(path, self.settings.allowed_local_roots)
            os.close(descriptor)
            local = Path(path)
            label = name or local.name
        else:
            raise OveError("invalid_input", "Supply either asset_id or path.")
        result = self.design.upload_asset(local, label)
        return {**result, "local_name": label}

    def canva_export_design(
        self,
        design_id: str,
        format_type: str = "png",
        quality: str | None = None,
        register_assets: bool = True,
    ) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="ove-canva-") as directory:
            exported = self.design.export_design(design_id, format_type, Path(directory), quality)
            assets = []
            for entry in exported["files"]:
                if not register_assets:
                    break
                asset = self.register_file_asset(
                    Path(entry["path"]), f"canva-{design_id}.{format_type}", "canva_export"
                )
                assets.append({"asset_id": asset["id"], "bytes": asset["bytes"]})
        return {
            "export_id": exported["export_id"],
            "format": exported["format"],
            "file_count": len(exported["files"]),
            "assets": assets,
            "note": exported["note"],
        }
