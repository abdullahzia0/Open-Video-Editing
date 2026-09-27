import os
import shutil
from pathlib import Path

import pytest

from ove.application.bootstrap import build_service
from ove.config import Settings


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        data_dir=tmp_path / "data",
        allowed_local_roots=[tmp_path],
        ffmpeg_path=os.environ.get("OVE_TEST_FFMPEG", shutil.which("ffmpeg") or "ffmpeg"),
        ffprobe_path=os.environ.get("OVE_TEST_FFPROBE", shutil.which("ffprobe") or "ffprobe"),
        job_timeout_seconds=30,
    )


@pytest.fixture
def service(settings):
    return build_service(settings)


@pytest.fixture
def media_service(service):
    if not shutil.which(service.settings.ffmpeg_path) or not shutil.which(
        service.settings.ffprobe_path
    ):
        if os.environ.get("OVE_REQUIRE_MEDIA_TESTS") == "1":
            pytest.fail("FFmpeg and ffprobe are required for this test run")
        pytest.skip("FFmpeg and ffprobe not installed")
    return service


@pytest.fixture
def clip(media_service, tmp_path) -> Path:
    from ove.utilities.process import run_process

    output = tmp_path / "input.mp4"
    run_process(
        [
            media_service.settings.ffmpeg_path,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=25:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(output),
        ],
        20,
    )
    return output
