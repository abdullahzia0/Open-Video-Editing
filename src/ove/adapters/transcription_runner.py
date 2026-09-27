"""Isolated ASR process; the worker enforces cancellation and a wall-clock limit."""

import sys
from pathlib import Path

from ove.adapters.faster_whisper import FasterWhisperProvider
from ove.config import Settings
from ove.utilities.identity import canonical


def main() -> None:
    configuration, source, destination, language = sys.argv[1:]
    settings = Settings.model_validate_json(Path(configuration).read_text())
    result = FasterWhisperProvider(settings).transcribe(Path(source), language or None)
    Path(destination).write_text(canonical(result))


if __name__ == "__main__":
    main()
