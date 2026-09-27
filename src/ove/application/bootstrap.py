"""Composition root: the only module that chooses concrete adapters."""

from ove.adapters.canva import CanvaRestAdapter
from ove.adapters.faster_whisper import FasterWhisperProvider
from ove.adapters.ffmpeg import FFmpegEngine
from ove.adapters.local_storage import LocalBlobStore
from ove.adapters.sqlite import SQLiteRepository
from ove.adapters.unavailable import UnavailableTranscription, UnavailableTranslation
from ove.application.service import EditingService
from ove.config import Settings
from ove.formats.registry import PresetRegistry
from ove.ports.providers import TranscriptionProvider
from ove.security.secrets import SecretStore


def build_service(settings: Settings | None = None) -> EditingService:
    settings = settings or Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    provider: TranscriptionProvider = UnavailableTranscription()
    if settings.transcription_provider == "faster-whisper":
        provider = FasterWhisperProvider(settings)
    return EditingService(
        settings,
        SQLiteRepository(settings.data_dir / "metadata.sqlite3"),
        LocalBlobStore(settings),
        FFmpegEngine(settings),
        PresetRegistry(settings.preset_dir),
        CanvaRestAdapter(
            settings, SecretStore(settings.data_dir / "credentials.enc", settings.secret_key)
        ),
        provider,
        UnavailableTranslation(),
    )
