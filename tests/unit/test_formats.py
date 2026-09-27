import pytest
from pydantic import ValidationError

from ove.domain.errors import OveError
from ove.formats.registry import PresetRegistry


def test_composition_and_provenance():
    result = PresetRegistry().resolve(["portrait-1080", "h264-mp4", "quality"])
    assert result["export"]["width"] == 1080
    assert result["export"]["audio"] == "aac"
    assert result["provenance"]["width"] == "portrait-1080"


def test_conflict_requires_override():
    registry = PresetRegistry()
    with pytest.raises(OveError, match="disagree"):
        registry.resolve(["draft", "quality"])
    resolved = registry.resolve(["draft", "quality"], {"crf": 22, "encoder_preset": "fast"})
    assert resolved["export"]["crf"] == 22


def test_thousands_of_custom_combinations_use_one_resolver():
    registry = PresetRegistry()
    count = 0
    for width in range(320, 352, 2):
        for height in range(240, 256, 2):
            for crf in range(18, 26):
                result = registry.resolve(
                    ["h264-mp4"], {"width": width, "height": height, "crf": crf}
                )
                assert result["export"]["width"] == width
                count += 1
    assert count == 1024


@pytest.mark.parametrize("overrides", [{"width": 7, "height": 9}, {"codec": "fake"}])
def test_invalid_presets(overrides):
    with pytest.raises(ValidationError):
        PresetRegistry().resolve([], overrides)
