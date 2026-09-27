"""Hard technical acceptance checks; no claim to prove subjective quality."""

from ove.domain.models import Check, ExpectedMedia, MediaInfo, ValidationReport


def validate_output(
    expected: ExpectedMedia, actual: MediaInfo, source: MediaInfo
) -> ValidationReport:
    tolerance = max(0.15, 2 * source.frame_seconds)
    return ValidationReport(
        checks=[
            Check(
                name="dimensions",
                passed=(actual.width, actual.height) == (expected.width, expected.height),
                expected=f"{expected.width}x{expected.height}",
                actual=f"{actual.width}x{actual.height}",
            ),
            Check(
                name="duration",
                passed=abs(actual.duration - expected.duration) <= tolerance,
                expected=f"{expected.duration} +/- {tolerance}s",
                actual=str(actual.duration),
            ),
            Check(
                name="audio_presence",
                passed=bool(actual.audio_codec) == expected.has_audio,
                expected=str(expected.has_audio),
                actual=str(bool(actual.audio_codec)),
            ),
            Check(
                name="video_codec",
                passed=actual.video_codec == "h264",
                expected="h264",
                actual=actual.video_codec,
            ),
        ],
        warnings=[
            "Technical validation does not establish aesthetic quality, "
            "caption legibility or speech accuracy.",
            "Full decode checks corruption; "
            "automated perceptual A/V sync analysis is not implemented.",
        ],
    )
