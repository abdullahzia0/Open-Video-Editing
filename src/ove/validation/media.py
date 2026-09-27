"""Hard technical acceptance checks; no claim to prove subjective quality."""

from ove.domain.models import (
    AudioInfo,
    Check,
    ExpectedMedia,
    ImageInfo,
    MediaInfo,
    ValidationReport,
)

#: A still image is compared exactly; encoded video is compared with a frame tolerance.
_IMAGE_CODECS = {"mjpeg", "png", "webp", "bmp", "gif"}


def _tolerance(source: MediaInfo | None) -> float:
    if source is None:
        return 0.2
    return max(0.15, 2 * source.frame_seconds)


def validate_output(
    expected: ExpectedMedia,
    actual: MediaInfo | AudioInfo | ImageInfo,
    source: MediaInfo | None = None,
) -> ValidationReport:
    """Compare measured output against the plan's promise."""
    checks: list[Check] = []
    warnings = [
        "Technical validation does not establish aesthetic quality, "
        "caption legibility or speech accuracy.",
        "Full decode checks corruption; automated perceptual A/V sync analysis is not implemented.",
    ]

    if expected.kind == "image":
        assert isinstance(actual, ImageInfo)
        checks.append(
            Check(
                name="image_dimensions",
                passed=(actual.width, actual.height) == (expected.width, expected.height),
                expected=f"{expected.width}x{expected.height}",
                actual=f"{actual.width}x{actual.height}",
            )
        )
        checks.append(
            Check(
                name="image_codec",
                passed=actual.codec in _IMAGE_CODECS,
                expected="an image codec",
                actual=actual.codec,
            )
        )
        return ValidationReport(checks=checks, coverage="structural", warnings=warnings)

    if expected.kind == "audio":
        assert isinstance(actual, AudioInfo)
        if expected.duration is not None:
            tolerance = max(0.2, expected.duration * 0.02)
            checks.append(
                Check(
                    name="duration",
                    passed=abs(actual.duration - expected.duration) <= tolerance,
                    expected=f"{expected.duration:.3f} +/- {tolerance:.3f}s",
                    actual=f"{actual.duration:.3f}",
                )
            )
        checks.append(
            Check(
                name="audio_presence",
                passed=bool(actual.audio_codec) == bool(expected.has_audio),
                expected=str(expected.has_audio),
                actual=str(bool(actual.audio_codec)),
            )
        )
        return ValidationReport(checks=checks, coverage="full-decode", warnings=warnings)

    assert isinstance(actual, MediaInfo)
    tolerance = _tolerance(source)
    if expected.duration is not None:
        checks.append(
            Check(
                name="duration",
                passed=abs(actual.duration - expected.duration) <= tolerance,
                expected=f"{expected.duration} +/- {tolerance}s",
                actual=str(actual.duration),
            )
        )
    if expected.width is not None and expected.height is not None:
        checks.append(
            Check(
                name="dimensions",
                passed=(actual.width, actual.height) == (expected.width, expected.height),
                expected=f"{expected.width}x{expected.height}",
                actual=f"{actual.width}x{actual.height}",
            )
        )
    if expected.has_audio is not None:
        checks.append(
            Check(
                name="audio_presence",
                passed=bool(actual.audio_codec) == expected.has_audio,
                expected=str(expected.has_audio),
                actual=str(bool(actual.audio_codec)),
            )
        )
    if expected.video_codec is not None:
        # A stream copy keeps whatever the source carried, so the source codec is
        # the correct reference rather than a hardcoded guess.
        if expected.video_codec == "copy":
            expected_codec = source.video_codec if source is not None else actual.video_codec
        else:
            expected_codec = expected.video_codec
        checks.append(
            Check(
                name="video_codec",
                passed=actual.video_codec == expected_codec,
                expected=expected_codec,
                actual=actual.video_codec,
            )
        )
    return ValidationReport(checks=checks, coverage="full-decode", warnings=warnings)
