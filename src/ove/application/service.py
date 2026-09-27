"""Application use cases; callable from CLI, MCP or another trusted local client."""

import hashlib
from typing import Any

from ove.application.planning import validate_plan
from ove.config import Settings
from ove.domain.errors import OveError
from ove.domain.models import MediaInfo, PlanRequest
from ove.formats.registry import PresetRegistry
from ove.ports.media import MediaEngine
from ove.ports.providers import DesignProvider, TranscriptionProvider
from ove.ports.repository import Repository
from ove.ports.storage import BlobStore
from ove.utilities.identity import digest, new_id, now


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
    ):
        self.settings, self.repository, self.storage = settings, repository, storage
        self.engine, self.presets, self.design = engine, presets, design
        self.transcription = transcription

    def capabilities(self) -> dict[str, Any]:
        return {
            "schema_version": "1",
            "mode": "local-single-user",
            "media": self.engine.capabilities(),
            "canva": self.design.capabilities(),
            "transcription": self.transcription.capabilities(),
            "unsupported": [
                "merge",
                "freeze",
                "stabilization",
                "neural_upscale",
                "translation",
                "word_highlighting",
                "arbitrary_motion_scenes",
                "remote_asset_import",
                "hosted_multi_tenant",
                "social_publishing",
                "file_size_target",
            ],
        }

    def import_asset(self, source: str) -> dict[str, Any]:
        key, checksum, size = self.storage.ingest(source)
        info = self.engine.probe(self.storage.path(key))
        asset: dict[str, Any] = {
            "id": new_id("asset"),
            "blob": key,
            "sha256": checksum,
            "bytes": size,
            "media": info.model_dump(mode="json"),
            "created_at": now(),
        }
        self.repository.put("asset", asset["id"], asset)
        return asset

    def create_project(self, name: str, asset_ids: list[str]) -> dict[str, Any]:
        if not name.strip() or len(name) > 200 or not 1 <= len(asset_ids) <= 100:
            raise OveError("invalid_project", "Provide a name and 1–100 asset references.")
        for identifier in asset_ids:
            self.repository.get("asset", identifier)
        project: dict[str, Any] = {
            "id": new_id("project"),
            "name": name,
            "revision": 1,
            "asset_ids": list(dict.fromkeys(asset_ids)),
            "created_at": now(),
        }
        self.repository.put("project", project["id"], project)
        self.repository.put("revision", f"{project['id']}:1", project)
        return project

    def revise_project(
        self, project_id: str, expected: int, asset_ids: list[str]
    ) -> dict[str, Any]:
        if not 1 <= len(asset_ids) <= 100:
            raise OveError("invalid_project", "Provide 1–100 asset references.")
        for identifier in asset_ids:
            self.repository.get("asset", identifier)
        return self.repository.revise_project(project_id, expected, list(dict.fromkeys(asset_ids)))

    def create_plan(self, request: PlanRequest) -> dict[str, Any]:
        project = self.repository.get("project", request.project_id)
        if request.revision != project["revision"]:
            raise OveError(
                "revision_conflict", "Fetch the current project revision before planning."
            )
        if request.asset_id not in project["asset_ids"]:
            raise OveError("invalid_asset", "Asset is not part of this project revision.")
        asset = self.repository.get("asset", request.asset_id)
        info = MediaInfo.model_validate(asset["media"])
        expected = validate_plan(request, info, self.settings.max_pixels)
        capabilities = self.engine.capabilities()
        self._check_engine(request, capabilities)
        body = request.model_dump(mode="json")
        # Sort a set explicitly; canonical hashes must survive process hash randomization.
        body["intent"]["allowed_effects"] = sorted(request.intent.allowed_effects)
        warnings = ["Video is re-encoded to H.264; source blobs remain unchanged."]
        if any(op.type in {"trim", "speed"} for op in request.operations) and info.audio_codec:
            warnings.append("Temporal edits re-encode audio to AAC to preserve edited timing.")
        if any(op.type == "speed" for op in request.operations):
            warnings.append(
                "Speed changes resample video to a constant frame rate of source rate x factor; "
                "variable-frame-rate timestamps are not preserved."
            )
        if any(op.type in {"captions", "title"} for op in request.operations):
            warnings.append(
                "Text uses installed font fallback; visually review glyphs and line wrapping."
            )
        plan = {
            "id": new_id("plan"),
            "request": body,
            "expected": expected.model_dump(mode="json"),
            "engine": capabilities,
            "source_sha256": asset["sha256"],
            "warnings": warnings,
            "created_at": now(),
        }
        plan["hash"] = digest({"request": body, "source": asset["sha256"], "engine": capabilities})
        self.repository.put("plan", plan["id"], plan)
        return plan

    @staticmethod
    def _check_engine(request: PlanRequest, capabilities: dict[str, object]) -> None:
        if not capabilities.get("available"):
            raise OveError(
                "missing_dependency",
                "FFmpeg with libx264 is not available.",
                "Install the required engine and inspect system_capabilities.",
            )
        supported = capabilities.get("operations", [])
        if not isinstance(supported, list):
            raise OveError("invalid_capabilities", "Engine returned malformed capabilities.")
        missing = {op.type for op in request.operations} - set(supported)
        if request.export.width is not None and "resize" not in supported:
            missing.add("resize")
        if missing:
            raise OveError(
                "unsupported_operation", f"Unavailable engine operations: {sorted(missing)}"
            )
        needs_aac = request.export.audio == "aac" or any(
            op.type in {"trim", "speed"} for op in request.operations
        )
        encoders = capabilities.get("encoders", [])
        if needs_aac and (not isinstance(encoders, list) or "aac" not in encoders):
            raise OveError("missing_dependency", "The selected engine has no AAC encoder.")

    def validate_plan(self, plan_id: str, plan_hash: str) -> dict[str, Any]:
        plan = self.repository.get("plan", plan_id)
        if plan["hash"] != plan_hash:
            raise OveError("plan_mismatch", "Plan hash does not match the immutable plan.")
        request = PlanRequest.model_validate(plan["request"])
        asset = self.repository.get("asset", request.asset_id)
        expected = validate_plan(
            request, MediaInfo.model_validate(asset["media"]), self.settings.max_pixels
        )
        capabilities = self.engine.capabilities()
        self._check_engine(request, capabilities)
        if capabilities != plan["engine"]:
            raise OveError(
                "engine_changed",
                "Engine capabilities changed after planning.",
                "Create a new plan to capture the new engine fingerprint.",
            )
        with self.storage.path(asset["blob"]).open("rb") as source:
            actual_hash = hashlib.file_digest(source, "sha256").hexdigest()
        if actual_hash != plan["source_sha256"]:
            raise OveError("source_changed", "Stored source no longer matches its plan checksum.")
        return {
            "ready": True,
            "plan_id": plan_id,
            "hash": plan_hash,
            "expected": expected.model_dump(mode="json"),
            "warnings": plan["warnings"],
        }

    def submit_render(self, plan_id: str, plan_hash: str, key: str) -> dict[str, Any]:
        self.validate_plan(plan_id, plan_hash)
        return self.repository.submit(
            "render", {"plan_id": plan_id, "hash": plan_hash}, key, plan_hash
        )

    def submit_transcript(self, asset_id: str, language: str | None, key: str) -> dict[str, Any]:
        asset = self.repository.get("asset", asset_id)
        if not asset["media"].get("audio_codec"):
            raise OveError("no_audio", "This asset has no audio stream.")
        if not self.transcription.capabilities().get("available"):
            raise OveError(
                "provider_unavailable",
                "Local ASR is not configured.",
                "Install the transcription extra and configure local model files.",
            )
        payload = {"asset_id": asset_id, "language": language}
        return self.repository.submit("transcribe", payload, key, digest(payload))

    def artifact(self, artifact_id: str) -> dict[str, Any]:
        artifact = self.repository.get("artifact", artifact_id)
        job = self.repository.job(artifact["job_id"])
        if job["state"] != "succeeded":
            raise OveError("artifact_not_ready", "The producing job has not succeeded.")
        return {**artifact, "local_path": str(self.storage.path(artifact["blob"]))}

    def retry(self, job_id: str, key: str) -> dict[str, Any]:
        job = self.repository.job(job_id)
        if key == job["idempotency_key"]:
            raise OveError("idempotency_conflict", "A retry requires a new idempotency key.")
        if job["state"] not in {"failed", "validation_failed", "cancelled"}:
            raise OveError("not_retryable", "Only failed or cancelled jobs can be retried.")
        if job["kind"] == "render":
            return self.submit_render(job["payload"]["plan_id"], job["payload"]["hash"], key)
        if job["kind"] == "transcribe":
            return self.submit_transcript(
                job["payload"]["asset_id"], job["payload"]["language"], key
            )
        raise OveError("unsupported_operation", "This job kind cannot be retried.")
