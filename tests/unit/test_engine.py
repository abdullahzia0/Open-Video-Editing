"""Pure filter/rate compilation checks; no media engine required."""

from fractions import Fraction

from ove.adapters.ffmpeg import frame_rate_ratio, resize_filter


def test_frame_rate_ratio_parses_ffprobe_values():
    assert frame_rate_ratio("25/1") == Fraction(25)
    assert frame_rate_ratio("30000/1001") == Fraction(30000, 1001)
    assert frame_rate_ratio("25") == Fraction(1, 25)


def test_frame_rate_ratio_falls_back_for_unknown_values():
    assert frame_rate_ratio("0/0") == Fraction(1, 25)
    assert frame_rate_ratio("N/A") == Fraction(1, 25)
    assert frame_rate_ratio("-25/1") == Fraction(1, 25)


def test_resize_filter_modes_are_distinct():
    assert resize_filter(1080, 1920, "stretch") == "scale=1080:1920:flags=lanczos,setsar=1"
    assert "force_original_aspect_ratio=increase" in resize_filter(1080, 1920, "crop")
    assert resize_filter(1080, 1920, "crop").endswith("crop=1080:1920,setsar=1")
    assert "force_original_aspect_ratio=decrease" in resize_filter(1080, 1920, "pad")
    assert resize_filter(1080, 1920, "pad").endswith("pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1")
