"""Optional, offline-only model loading. Importing the core never imports ML libraries."""

import importlib.util
from pathlib import Path
from typing import Any

from ove.config import Settings
from ove.domain.errors import OveError


class FasterWhisperProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    def capabilities(self) -> dict[str, Any]:
        model = self.settings.transcription_model_dir
        ready = bool(model and (model / "model.bin").is_file())
        installed = importlib.util.find_spec("faster_whisper") is not None
        return {
            "provider": "faster-whisper",
            "available": ready and installed,
            "reason": None if ready and installed else "missing_package_or_local_model",
            "offline": True,
        }

    def transcribe(self, audio: Path, language: str | None) -> dict[str, Any]:
        if not self.capabilities()["available"]:
            raise OveError(
                "missing_dependency",
                "faster-whisper or local model files are unavailable.",
                "Run uv sync --extra transcription and set OVE_TRANSCRIPTION_MODEL_DIR "
                "to a trusted, complete CTranslate2 model directory containing model.bin.",
            )
        from faster_whisper import WhisperModel  # type: ignore[import-not-found]

        model = WhisperModel(
            str(self.settings.transcription_model_dir),
            device=self.settings.transcription_device,
            compute_type=self.settings.transcription_compute_type,
            local_files_only=True,
        )
        segments, info = model.transcribe(str(audio), language=language, word_timestamps=True)
        return {
            "language": info.language,
            "provider": "faster-whisper",
            "timing_method": "model_estimated",
            "confidence": None,
            "segments": [
                {
                    "start": s.start,
                    "end": s.end,
                    "text": s.text,
                    "words": [
                        {
                            "start": w.start,
                            "end": w.end,
                            "text": w.word,
                            "confidence": w.probability,
                        }
                        for w in (s.words or [])
                    ],
                }
                for s in segments
            ],
        }
