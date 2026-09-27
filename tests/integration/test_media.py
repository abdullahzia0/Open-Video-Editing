import hashlib

import pytest

from ove.application.worker import Worker
from ove.domain.errors import OveError
from ove.domain.models import PlanRequest

pytestmark = pytest.mark.media


def prepare(service, clip, operations, effects, export=None):
    asset = service.import_asset(str(clip))
    project = service.create_project("Test", [asset["id"]])
    request = PlanRequest.model_validate(
        {
            "project_id": project["id"],
            "revision": 1,
            "asset_id": asset["id"],
            "intent": {"request": "Test requested edit", "allowed_effects": effects},
            "operations": operations,
            "export": export or {"encoder_preset": "ultrafast"},
        }
    )
    return service.create_plan(request)


def execute(service, plan):
    job = service.submit_render(plan["id"], plan["hash"], plan["id"])
    Worker(service).run(once=True)
    finished = service.repository.job(job["id"])
    assert finished["state"] == "succeeded", finished
    return service.artifact(finished["result"]["artifact_id"])


def test_lighting_full_pipeline_preserves_source(media_service, clip):
    original = hashlib.sha256(clip.read_bytes()).hexdigest()
    plan = prepare(media_service, clip, [{"type": "lighting", "gamma": 1.1}], ["lighting"])
    artifact = execute(media_service, plan)
    assert artifact["bytes"] > 0
    assert all(check["passed"] for check in artifact["validation"]["checks"])
    assert hashlib.sha256(clip.read_bytes()).hexdigest() == original
    assert artifact["sha256"] != original


@pytest.mark.parametrize(
    "operations,effects,width,height,duration",
    [
        ([{"type": "trim", "start": 0.5, "end": 1.5}], ["timing"], 320, 240, 1),
        ([{"type": "speed", "factor": 2}], ["timing"], 320, 240, 1),
        ([{"type": "speed", "factor": 0.5}], ["timing"], 320, 240, 4),
        (
            [{"type": "resize", "width": 180, "height": 320, "fit": "pad"}],
            ["geometry"],
            180,
            320,
            2,
        ),
        ([{"type": "rotate", "degrees": 90}], ["geometry"], 240, 320, 2),
        (
            [{"type": "crop", "x": 0, "y": 0, "width": 160, "height": 160}],
            ["geometry"],
            160,
            160,
            2,
        ),
        (
            [{"type": "title", "text": "Hello world", "start": 0, "end": 1, "animated": True}],
            ["text"],
            320,
            240,
            2,
        ),
        (
            [{"type": "captions", "cues": [{"start": 0, "end": 1, "text": "Hello"}]}],
            ["text"],
            320,
            240,
            2,
        ),
    ],
)
def test_operations(media_service, clip, operations, effects, width, height, duration):
    plan = prepare(media_service, clip, operations, effects)
    artifact = execute(media_service, plan)
    info = media_service.engine.probe(media_service.storage.path(artifact["blob"]))
    assert (info.width, info.height) == (width, height)
    assert abs(info.duration - duration) < 0.15


def test_speed_change_resamples_without_dts_collisions(media_service, clip):
    plan = prepare(media_service, clip, [{"type": "speed", "factor": 2}], ["timing"])
    artifact = execute(media_service, plan)
    info = media_service.engine.probe(media_service.storage.path(artifact["blob"]))
    numerator, denominator = (int(part) for part in info.frame_rate.split("/"))
    assert numerator / denominator == 50


def test_hash_mismatch_and_stale_revision(media_service, clip):
    plan = prepare(media_service, clip, [], [])
    with pytest.raises(OveError, match="hash"):
        media_service.submit_render(plan["id"], "wrong", "bad")
    project_id = plan["request"]["project_id"]
    media_service.revise_project(project_id, 1, [plan["request"]["asset_id"]])
    with pytest.raises(OveError, match="revision"):
        media_service.create_plan(PlanRequest.model_validate(plan["request"]))


def test_failed_engine_never_reports_success(media_service, clip):
    plan = prepare(media_service, clip, [], [])
    job = media_service.submit_render(plan["id"], plan["hash"], "failure")
    media_service.settings.ffmpeg_path = "/does/not/exist"
    Worker(media_service).run(once=True)
    finished = media_service.repository.job(job["id"])
    assert finished["state"] == "failed"
    assert finished["result"] is None


def test_playlist_demuxer_rejected(media_service, clip, tmp_path):
    playlist = tmp_path / "input.ffconcat"
    playlist.write_text(f"ffconcat version 1.0\nfile '{clip}'\n")
    with pytest.raises(OveError):
        media_service.import_asset(str(playlist))


def test_stored_source_tampering_rejected(media_service, clip):
    plan = prepare(media_service, clip, [], [])
    asset = media_service.repository.get("asset", plan["request"]["asset_id"])
    path = media_service.storage.path(asset["blob"])
    path.chmod(0o600)
    path.write_bytes(b"modified")
    with pytest.raises(OveError, match="checksum"):
        media_service.submit_render(plan["id"], plan["hash"], "tampered")
