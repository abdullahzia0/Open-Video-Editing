"""Explicit unavailable adapters; never simulate provider success."""

from pathlib import Path
from typing import Any

from ove.domain.errors import OveError


class UnavailableCanva:
    def capabilities(self) -> dict[str, Any]:
        return {
            "provider": "canva",
            "available": False,
            "reason": "adapter_not_implemented",
            "action": "Implement an authorized Canva REST adapter; see docs/providers.md. "
            "Credentials alone do not enable this adapter.",
        }

    def execute(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise OveError(
            "provider_unavailable",
            "Canva operations are not implemented in this release.",
            "Use the rendered file manually in Canva, or implement the DesignProvider contract "
            "with registered OAuth credentials and verified operation support.",
        )


class UnavailableTranscription:
    def capabilities(self) -> dict[str, Any]:
        return {"provider": "transcription", "available": False, "reason": "disabled"}

    def transcribe(self, audio: Path, language: str | None) -> dict[str, Any]:
        raise OveError(
            "provider_unavailable",
            "Local transcription is disabled.",
            "Install the transcription extra and configure a local faster-whisper model directory.",
        )
