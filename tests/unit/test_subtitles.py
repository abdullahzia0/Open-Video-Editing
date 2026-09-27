import pytest

from ove.captions.subtitles import serialize, timestamp
from ove.domain.models import CaptionCue, Title
from ove.motion.ass import compose


def test_rollover_and_sidecars():
    assert timestamp(59.9999) == "00:01:00,000"
    cue = CaptionCue(start=0, end=1.2, text="Hello\nworld")
    assert "00:00:01,200" in serialize([cue], "srt")
    assert serialize([cue], "vtt").startswith("WEBVTT\n\n")
    assert "Hello\nworld" in serialize([cue], "vtt")


def test_animated_title_has_bounded_fade():
    text = compose([Title(text="Welcome", start=0, end=2, animated=True)], 1920, 1080)
    assert r"\fad(150,150)" in text
    assert "0:00:02.00" in text


def test_unknown_format():
    with pytest.raises(ValueError):
        serialize([], "unknown")
