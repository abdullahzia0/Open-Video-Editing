import pytest
from pydantic import ValidationError

from ove.application.planning import validate_plan
from ove.domain.errors import OveError
from ove.domain.models import MediaInfo, PlanRequest


def media(**kwargs):
    return MediaInfo(
        duration=10,
        width=1920,
        height=1080,
        video_codec="h264",
        pixel_format="yuv420p",
        frame_rate="25/1",
        **kwargs,
    )


def request(operations, allowed=("lighting",), **export):
    return PlanRequest.model_validate(
        {
            "project_id": "p",
            "revision": 1,
            "asset_id": "a",
            "operations": operations,
            "intent": {"request": "Only improve lighting", "allowed_effects": list(allowed)},
            "export": export,
        }
    )


def test_lighting_preserves_geometry_and_duration():
    expected = validate_plan(request([{"type": "lighting", "gamma": 1.1}]), media(), 9_000_000)
    assert (expected.width, expected.height, expected.duration) == (1920, 1080, 10)


@pytest.mark.parametrize(
    "op",
    [
        {"type": "resize", "width": 1080, "height": 1920},
        {"type": "title", "text": "Hello", "start": 0, "end": 2},
        {"type": "sharpen"},
        {"type": "denoise"},
    ],
)
def test_lighting_rejects_scope_expansion(op):
    with pytest.raises(OveError, match="exceed allowed effects"):
        validate_plan(request([op]), media(), 9_000_000)


def test_export_geometry_cannot_bypass_intent():
    with pytest.raises(OveError):
        validate_plan(request([], width=1080, height=1920), media(), 9_000_000)


def test_sequential_timing_and_dimensions():
    expected = validate_plan(
        request(
            [
                {"type": "trim", "start": 2, "end": 6},
                {"type": "speed", "factor": 2},
                {"type": "rotate", "degrees": 90},
            ],
            ("timing", "geometry"),
        ),
        media(),
        9_000_000,
    )
    assert expected.duration == 2
    assert (expected.width, expected.height) == (1080, 1920)


@pytest.mark.parametrize(
    "ops",
    [
        [{"type": "trim", "start": 0, "end": 20}],
        [{"type": "crop", "x": 1800, "y": 0, "width": 200, "height": 100}],
        [{"type": "title", "text": "x", "start": 0, "end": 11}],
        [{"type": "title", "text": "x", "start": 0, "end": 1}, {"type": "speed", "factor": 2}],
    ],
)
def test_invalid_ranges_and_order(ops):
    with pytest.raises(OveError):
        validate_plan(request(ops, ("timing", "geometry", "text")), media(), 9_000_000)


@pytest.mark.parametrize(
    "op",
    [
        {"type": "lighting", "gamma": float("nan")},
        {"type": "lighting", "raw_filter": "movie=/etc/passwd"},
        {"type": "shell", "command": "echo hello"},
        {"type": "trim", "start": 3, "end": 2},
    ],
)
def test_strict_operation_contracts(op):
    with pytest.raises(ValidationError):
        request([op])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rotation": 90},
        {"sample_aspect_ratio": "4:3"},
        {"color_transfer": "smpte2084"},
        {"audio_streams": 2},
        {"other_streams": 1},
    ],
)
def test_unhandled_source_characteristics_fail(kwargs):
    with pytest.raises(OveError):
        validate_plan(request([]), media(**kwargs), 9_000_000)
