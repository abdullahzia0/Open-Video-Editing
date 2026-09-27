"""Exclusive local worker, persistent jobs, truthful terminal states."""

import fcntl
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ove.application.service import EditingService
from ove.domain.errors import OveError
from ove.domain.models import Check, ExpectedMedia, MediaInfo, PlanRequest
from ove.utilities.identity import new_id, now
from ove.utilities.process import run_process
from ove.validation.media import validate_output

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, service: EditingService):
        self.service = service

    def run(self, once: bool = False) -> None:
        lock_path = self.service.settings.data_dir / "worker.lock"
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OveError(
                    "worker_busy", "Another local worker already owns this data directory."
                ) from exc
            self.service.repository.recover()
            while True:
                worked = self.run_one()
                if once:
                    return
                if not worked:
                    time.sleep(0.5)

    def run_one(self) -> bool:
        repository = self.service.repository
        job = repository.claim()
        if job is None:
            return False
        try:
            if job["kind"] == "render":
                self._render(job)
            elif job["kind"] == "transcribe":
                self._transcribe(job)
            else:
                raise OveError("unsupported_job", "Unknown job kind.")
        except OveError as exc:
            state = "cancelled" if exc.code == "cancelled" else "failed"
            if exc.code == "validation_failed":
                state = "validation_failed"
            repository.transition(job["id"], state, error=exc.as_dict())
        except Exception:
            logger.exception("Unexpected worker failure for job %s", job["id"])
            repository.transition(
                job["id"],
                "failed",
                error={
                    "code": "internal_error",
                    "message": "Worker failed unexpectedly.",
                    "action": "Review local worker logs.",
                    "retryable": False,
                },
            )
        return True

    def _cancelled(self, job_id: str) -> bool:
        return self.service.repository.job(job_id)["state"] in {"cancel_requested", "cancelled"}

    def _render(self, job: dict[str, Any]) -> None:
        service = self.service
        payload = job["payload"]
        service.validate_plan(payload["plan_id"], payload["hash"])
        plan = service.repository.get("plan", payload["plan_id"])
        request = PlanRequest.model_validate(plan["request"])
        asset = service.repository.get("asset", request.asset_id)
        info = MediaInfo.model_validate(asset["media"])
        with tempfile.TemporaryDirectory(dir=service.settings.data_dir / "scratch") as directory:
            output = Path(directory) / f"output.{request.export.container}"
            service.engine.render(
                service.storage.path(asset["blob"]),
                output,
                info,
                request.operations,
                request.export,
                lambda: self._cancelled(job["id"]),
            )
            service.repository.transition(job["id"], "validating")
            if self._cancelled(job["id"]):
                raise OveError("cancelled", "Job was cancelled.")
            service.engine.decode(output, lambda: self._cancelled(job["id"]))
            report = validate_output(
                ExpectedMedia.model_validate(plan["expected"]), service.engine.probe(output), info
            )
            report.checks.append(
                Check(
                    name="full_decode",
                    passed=True,
                    expected="decodes without errors",
                    actual="passed",
                )
            )
            if (
                info.audio_codec
                and request.export.audio == "copy"
                and not any(op.type in {"trim", "speed"} for op in request.operations)
            ):
                original_hash = service.engine.audio_hash(
                    service.storage.path(asset["blob"]), lambda: self._cancelled(job["id"])
                )
                output_hash = service.engine.audio_hash(output, lambda: self._cancelled(job["id"]))
                report.checks.append(
                    Check(
                        name="copied_audio_payload",
                        passed=original_hash == output_hash,
                        expected=original_hash,
                        actual=output_hash,
                    )
                )
            if not report.passed:
                raise OveError(
                    "validation_failed",
                    json.dumps(report.model_dump(mode="json")),
                    "Inspect validation measurements; revise the plan if needed.",
                )
            key, checksum, size = service.storage.publish(output)
            artifact = {
                "id": new_id("artifact"),
                "blob": key,
                "sha256": checksum,
                "bytes": size,
                "job_id": job["id"],
                "plan_id": plan["id"],
                "extension": request.export.container,
                "validation": report.model_dump(mode="json"),
                "warnings": plan["warnings"],
                "created_at": now(),
            }
            service.repository.put("artifact", artifact["id"], artifact)
            service.repository.transition(
                job["id"], "succeeded", result={"artifact_id": artifact["id"]}
            )

    def _transcribe(self, job: dict[str, Any]) -> None:
        service = self.service
        asset = service.repository.get("asset", job["payload"]["asset_id"])
        with tempfile.TemporaryDirectory(dir=service.settings.data_dir / "scratch") as directory:
            root = Path(directory)
            audio, output, configuration = (
                root / "audio.wav",
                root / "transcript.json",
                root / "config.json",
            )
            run_process(
                [
                    service.settings.ffmpeg_path,
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-protocol_whitelist",
                    "file",
                    "-format_whitelist",
                    "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv",
                    "-i",
                    str(service.storage.path(asset["blob"])),
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(audio),
                ],
                service.settings.job_timeout_seconds,
                lambda: self._cancelled(job["id"]),
                audio,
                service.settings.max_output_bytes,
            )
            configuration.write_text(service.settings.model_dump_json())
            run_process(
                [
                    sys.executable,
                    "-m",
                    "ove.adapters.transcription_runner",
                    str(configuration),
                    str(audio),
                    str(output),
                    job["payload"]["language"] or "",
                ],
                service.settings.job_timeout_seconds,
                lambda: self._cancelled(job["id"]),
                output,
                10_000_000,
            )
            transcript = json.loads(output.read_text())
            transcript.update(
                id=new_id("transcript"), asset_id=asset["id"], job_id=job["id"], revision=1
            )
            service.repository.put("transcript", transcript["id"], transcript)
            service.repository.transition(
                job["id"], "succeeded", result={"transcript_id": transcript["id"]}
            )
