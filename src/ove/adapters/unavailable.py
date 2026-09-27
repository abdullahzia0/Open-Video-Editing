"""Explicit unavailable adapters; never simulate provider success."""

from pathlib import Path
from typing import Any

from ove.domain.errors import OveError


class UnavailableTranscription:
    def capabilities(self) -> dict[str, Any]:
        return {"provider": "transcription", "available": False, "reason": "disabled"}

    def transcribe(self, audio: Path, language: str | None) -> dict[str, Any]:
        raise OveError(
            "provider_unavailable",
            "Local transcription is disabled.",
            "Install the transcription extra and configure a local faster-whisper model directory.",
        )


class UnavailableTranslation:
    """Translation is not shipped. It is reported, never faked."""

    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": "translation",
            "available": False,
            "reason": "no_provider_configured",
            "action": "Configure a licensed translation provider before requesting translation. "
            "Transcribing and translating are separate capabilities; local ASR does not "
            "imply translation support.",
            "supported_languages": [],
        }

    def translate(self, segments: list[dict[str, Any]], target_language: str) -> dict[str, Any]:
        raise OveError(
            "provider_unavailable",
            "No translation provider is configured on this server.",
            "Translate the captions in your assistant, or configure a licensed provider. "
            "The original transcript is unchanged.",
        )
