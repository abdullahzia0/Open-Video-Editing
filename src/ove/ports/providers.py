"""Provider contracts. Implementations must disclose availability and failures."""

from pathlib import Path
from typing import Any, Protocol


class DesignProvider(Protocol):
    def capabilities(self) -> dict[str, Any]: ...
    def execute(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]: ...


class TranscriptionProvider(Protocol):
    def capabilities(self) -> dict[str, Any]: ...
    def transcribe(self, audio: Path, language: str | None) -> dict[str, Any]: ...
