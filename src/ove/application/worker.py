"""Exclusive local worker, persistent jobs, truthful terminal states.

The worker revalidates the immutable plan and the current engine before every
attempt. Outputs stay in scratch storage until every required check passes, so a
failed job never publishes a partial artifact as a success.
"""

import fcntl
import json
import logging
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from ove.application.service import TERMINAL_STATES, EditingService
from ove.domain.errors import OveError
from ove.domain.models import (
    AudioInfo,
    Check,
    ExpectedMedia,
    FrameSpec,
    ImageInfo,
    MediaInfo,
    MediaJobRequest,
    PlanRequest,
    ProbedMedia,
    SplitSpec,
)
from ove.utilities.identity import new_id, now
from ove.utilities.process import run_process
from ove.validation.media import validate_output

logger = logging.getLogger(__name__)

#: Operations that rewrite audio packets, so a payload-identity check cannot apply.
AUDIO_REWRITING_OPERATIONS = {"trim", "speed", "cut", "freeze"}


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
            handler = getattr(self, f"_job_{job['kind']}", None)
            if handler is None:
                raise OveError("unsupported_job", f"Unknown job kind: {job['kind']}")
            handler(job)
        except OveError as exc:
            state = "cancelled" if exc.code == "CANCELLED" else "failed"
            if exc.detail_code == "validation_failed":
                state = "validation_failed"
            repository.transition(job["id"], state, error=exc.as_dict())
        except Exception:
            logger.exception("Unexpected worker failure for job %s", job["id"])
            repository.transition(
                job["id"],
                "failed",
                error={
                    "code": "PROCESSING_FAILED",
                    "message": "Worker failed unexpectedly.",
                    "action": "Review local worker logs.",
                    "retryable": False,
                },
            )
        return True

    # ---------------------------------------------------------------------- helpers

    def _cancelled(self, job_id: str) -> bool:
        return self.service.repository.job(job_id)["state"] in {"cancel_requested", "cancelled"}

    def _scratch(self) -> tempfile.TemporaryDirectory[str]:
        return tempfile.TemporaryDirectory(dir=self.service.settings.data_dir / "scratch")

    def _plan(self, job: dict[str, Any]) -> dict[str, Any]:
        self.service.validate_plan(job["payload"]["plan_id"], job["payload"]["hash"])
        return self.service.repository.get("plan", job["payload"]["plan_id"])

    def _source(self, asset_id: str) -> Path:
        return self.service.storage.path(self.service.repository.get("asset", asset_id)["blob"])

    def _media(self, asset_id: str) -> ProbedMedia:
        return ProbedMedia.model_validate(self.service.repository.get("asset", asset_id)["media"])

    def _probe_kind(self, path: Path, kind: str) -> MediaInfo | AudioInfo | ImageInfo:
        if kind == "image":
            return self.service.engine.probe_image(path)
        if kind == "audio":
            return self.service.engine.probe_audio(path)
        return self.service.engine.probe(path)

    def _decode(self, path: Path, kind: str, job_id: str) -> None:
        if kind == "video":
            self.service.engine.decode(path, lambda: self._cancelled(job_id))
        else:
            self.service.engine.decode_audio(path, lambda: self._cancelled(job_id))

    def _publish(
        self,
        job: dict[str, Any],
        plan: dict[str, Any],
        outputs: list[Path],
        reports: list[Any],
        kind: str,
        extension: str,
    ) -> list[str]:
        ids = []
        for index, path in enumerate(outputs):
            report = reports[min(index, len(reports) - 1)]
            key, checksum, size = self.service.storage.publish(path)
            artifact = {
                "id": new_id("artifact"),
                "blob": key,
                "sha256": checksum,
                "bytes": size,
                "kind": kind,
                "job_id": job["id"],
                "plan_id": plan["id"],
                "extension": extension,
                "index": index + 1,
                "validation": report.model_dump(mode="json"),
                "warnings": plan["warnings"],
                "created_at": now(),
            }
            self.service.repository.put("artifact", artifact["id"], artifact)
            ids.append(artifact["id"])
        return ids

    def _validate_outputs(
        self,
        job: dict[str, Any],
        outputs: list[Path],
        expectations: list[ExpectedMedia],
        source: MediaInfo | None,
    ) -> list[Any]:
        reports = []
        for index, path in enumerate(outputs):
            expectation = expectations[min(index, len(expectations) - 1)]
            self._decode(path, expectation.kind, job["id"])
            if self._cancelled(job["id"]):
                raise OveError("cancelled", "Job was cancelled.")
            actual = self._probe_kind(path, expectation.kind)
            report = validate_output(expectation, actual, source)
            report.checks.append(
                Check(
                    name="full_decode",
                    passed=True,
                    expected="decodes without errors",
                    actual="passed",
                )
            )
            reports.append(report)
        return reports

    def _finish(
        self,
        job: dict[str, Any],
        plan: dict[str, Any],
        outputs: list[Path],
        expectations: list[ExpectedMedia],
        kind: str,
        extension: str,
        source: MediaInfo | None = None,
        extra_checks: list[Check] | None = None,
    ) -> None:
        self.service.repository.transition(job["id"], "validating")
        if self._cancelled(job["id"]):
            raise OveError("cancelled", "Job was cancelled.")
        reports = self._validate_outputs(job, outputs, expectations, source)
        if extra_checks:
            reports[0].checks.extend(extra_checks)
        if not all(report.passed for report in reports):
            raise OveError(
                "validation_failed",
                json.dumps([report.model_dump(mode="json") for report in reports]),
                "Inspect validation measurements; revise the plan if needed.",
            )
        ids = self._publish(job, plan, outputs, reports, kind, extension)
        self.service.repository.transition(
            job["id"],
            "succeeded",
            result={"artifact_id": ids[0], "artifact_ids": ids},
        )

    def _copied_audio_check(
        self, job: dict[str, Any], source: Path, output: Path, request: PlanRequest
    ) -> list[Check] | None:
        if request.export.audio != "copy":
            return None
        if any(operation.type in AUDIO_REWRITING_OPERATIONS for operation in request.operations):
            return None
        info = self._media(request.asset_id)
        if not info.has_audio:
            return None
        original = self.service.engine.audio_hash(source, lambda: self._cancelled(job["id"]))
        produced = self.service.engine.audio_hash(output, lambda: self._cancelled(job["id"]))
        return [
            Check(
                name="copied_audio_payload",
                passed=original == produced,
                expected=original,
                actual=produced,
            )
        ]

    # --------------------------------------------------------------------- handlers

    def _job_render(self, job: dict[str, Any]) -> None:
        service = self.service
        plan = self._plan(job)
        request = PlanRequest.model_validate(plan["request"])
        media = self._media(request.asset_id)
        assert media.video is not None
        expected = ExpectedMedia.model_validate(plan["expected"])
        source = self._source(request.asset_id)
        with self._scratch() as directory:
            output = Path(directory) / f"output.{request.export.container}"
            service.engine.render(
                source,
                output,
                media.video,
                request.operations,
                request.export,
                lambda: self._cancelled(job["id"]),
                expected_duration=expected.duration,
            )
            checks = self._copied_audio_check(job, source, output, request)
            self._finish(
                job,
                plan,
                [output],
                [expected],
                "video",
                request.export.container,
                source=media.video,
                extra_checks=checks,
            )

    def _job_concat(self, job: dict[str, Any]) -> None:
        plan = self._plan(job)
        request = MediaJobRequest.model_validate(plan["request"])
        spec = request.job
        assert spec.type == "concat"
        infos = []
        sources = []
        for identifier in request.asset_ids:
            media = self._media(identifier)
            assert media.video is not None
            infos.append(media.video)
            sources.append(self._source(identifier))
        expected = ExpectedMedia.model_validate(plan["expected"])
        with self._scratch() as directory:
            output = Path(directory) / f"output.{request.export.container}"
            self.service.engine.render_concat(
                sources,
                infos,
                output,
                spec,
                request.export,
                lambda: self._cancelled(job["id"]),
            )
            self._finish(
                job,
                plan,
                [output],
                [expected],
                "video",
                request.export.container,
                source=infos[0],
            )

    def _job_split(self, job: dict[str, Any]) -> None:
        plan = self._plan(job)
        request = MediaJobRequest.model_validate(plan["request"])
        spec = request.job
        assert isinstance(spec, SplitSpec)
        media = self._media(request.asset_ids[0])
        assert media.video is not None
        expected = ExpectedMedia.model_validate(plan["expected"])
        bounds = [0.0, *spec.boundaries, media.video.duration]
        expectations = [
            expected.model_copy(
                update={
                    "count": 1,
                    "duration": end - start,
                    "container": request.export.container,
                }
            )
            for start, end in zip(bounds, bounds[1:], strict=False)
        ]
        with self._scratch() as directory:
            outputs = self.service.engine.split_video(
                self._source(request.asset_ids[0]),
                Path(directory),
                media.video,
                spec.boundaries,
                request.export,
                lambda: self._cancelled(job["id"]),
            )
            self._finish(
                job,
                plan,
                outputs,
                expectations,
                "video",
                request.export.container,
                source=media.video,
            )

    def _job_frame(self, job: dict[str, Any]) -> None:
        plan = self._plan(job)
        request = MediaJobRequest.model_validate(plan["request"])
        spec = request.job
        assert isinstance(spec, FrameSpec)
        expected = ExpectedMedia.model_validate(plan["expected"])
        with self._scratch() as directory:
            output = Path(directory) / f"frame.{spec.format}"
            self.service.engine.extract_frame(
                self._source(request.asset_ids[0]),
                output,
                spec,
                lambda: self._cancelled(job["id"]),
            )
            self._finish(job, plan, [output], [expected], "image", spec.format)

    def _job_audio_extract(self, job: dict[str, Any]) -> None:
        plan = self._plan(job)
        request = MediaJobRequest.model_validate(plan["request"])
        spec = request.job
        assert spec.type == "audio_extract"
        expected = ExpectedMedia.model_validate(plan["expected"])
        with self._scratch() as directory:
            output = Path(directory) / f"audio.{spec.container}"
            self.service.engine.extract_audio(
                self._source(request.asset_ids[0]),
                output,
                spec,
                lambda: self._cancelled(job["id"]),
            )
            self._finish(job, plan, [output], [expected], "audio", spec.container)

    def _job_audio_process(self, job: dict[str, Any]) -> None:
        plan = self._plan(job)
        request = MediaJobRequest.model_validate(plan["request"])
        spec = request.job
        assert spec.type == "audio_process"
        media = self._media(request.asset_ids[0])
        expected = ExpectedMedia.model_validate(plan["expected"])
        container = (
            request.export.container
            if media.video is not None
            else (spec.container if spec.container != "keep" else "m4a")
        )
        with self._scratch() as directory:
            output = Path(directory) / f"audio.{container}"
            self.service.engine.process_audio(
                self._source(request.asset_ids[0]),
                output,
                spec,
                media,
                request.export,
                lambda: self._cancelled(job["id"]),
            )
            self._finish(
                job,
                plan,
                [output],
                [expected],
                "video" if media.video is not None else "audio",
                container,
                source=media.video,
            )

    def _job_audio_replace(self, job: dict[str, Any]) -> None:
        plan = self._plan(job)
        request = MediaJobRequest.model_validate(plan["request"])
        spec = request.job
        assert spec.type == "audio_replace"
        media = self._media(request.asset_ids[0])
        expected = ExpectedMedia.model_validate(plan["expected"])
        with self._scratch() as directory:
            output = Path(directory) / f"output.{request.export.container}"
            self.service.engine.replace_audio(
                self._source(request.asset_ids[0]),
                self._source(spec.audio_asset_id),
                output,
                spec,
                media,
                request.export,
                lambda: self._cancelled(job["id"]),
            )
            self._finish(
                job,
                plan,
                [output],
                [expected],
                "video",
                request.export.container,
                source=media.video,
            )

    def _job_audio_mix(self, job: dict[str, Any]) -> None:
        plan = self._plan(job)
        request = MediaJobRequest.model_validate(plan["request"])
        spec = request.job
        assert spec.type == "audio_mix"
        expected = ExpectedMedia.model_validate(plan["expected"])
        sources = [self._source(identifier) for identifier in request.asset_ids]
        container = request.export.container if spec.video_asset_id is not None else spec.container
        with self._scratch() as directory:
            output = Path(directory) / f"mix.{container}"
            self.service.engine.mix_audio(
                sources,
                output,
                spec,
                request.export,
                lambda: self._cancelled(job["id"]),
            )
            self._finish(
                job,
                plan,
                [output],
                [expected],
                "video" if spec.video_asset_id is not None else "audio",
                container,
            )

    def _job_overlay(self, job: dict[str, Any]) -> None:
        plan = self._plan(job)
        request = MediaJobRequest.model_validate(plan["request"])
        spec = request.job
        assert spec.type == "overlay"
        media = self._media(request.asset_ids[0])
        assert media.video is not None
        expected = ExpectedMedia.model_validate(plan["expected"])
        with self._scratch() as directory:
            output = Path(directory) / f"output.{request.export.container}"
            self.service.engine.render_overlay(
                self._source(request.asset_ids[0]),
                self._source(spec.overlay_asset_id),
                output,
                spec,
                media.video,
                request.export,
                lambda: self._cancelled(job["id"]),
            )
            self._finish(
                job,
                plan,
                [output],
                [expected],
                "video",
                request.export.container,
                source=media.video,
            )

    def _job_transcribe(self, job: dict[str, Any]) -> None:
        service = self.service
        asset = service.repository.get("asset", job["payload"]["asset_id"])
        with self._scratch() as directory:
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
                    "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv,mp3,wav,flac,aac,m4a",
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
            configuration.write_text(json.dumps(service.settings.transcription_config()))
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


def drain(service: EditingService, limit: int = 100) -> None:
    """Process queued work until the queue is empty. Used by tests and the CLI."""
    worker = Worker(service)
    for _ in range(limit):
        if not worker.run_one():
            return


__all__ = ["TERMINAL_STATES", "Worker", "drain"]
