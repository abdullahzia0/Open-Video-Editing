"""Composition root: the only module that chooses concrete adapters."""

from ove.adapters.faster_whisper import FasterWhisperProvider
from ove.adapters.ffmpeg import FFmpegEngine
from ove.adapters.local_storage import LocalBlobStore
from ove.adapters.sqlite import SQLiteRepository
from ove.adapters.unavailable import UnavailableCanva, UnavailableTranscription
from ove.application.service import EditingService
from ove.config import Settings
from ove.formats.registry import PresetRegistry
from ove.ports.providers import TranscriptionProvider


def build_service(settings: Settings | None = None) -> EditingService:
    settings = settings or Settings()
    provider: TranscriptionProvider = UnavailableTranscription()
    if settings.transcription_provider == "faster-whisper":
        provider = FasterWhisperProvider(settings)
    return EditingService(
        settings,
        SQLiteRepository(settings.data_dir / "metadata.sqlite3"),
        LocalBlobStore(settings),
        FFmpegEngine(settings),
        PresetRegistry(settings.preset_dir),
        UnavailableCanva(),
        provider,
    )
