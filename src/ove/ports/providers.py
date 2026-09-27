"""Provider contracts. Implementations must disclose availability and failures."""

from pathlib import Path
from typing import Any, Protocol


class DesignProvider(Protocol):
    """External design environment. Every operation must be verifiable or refused."""

    def capabilities(self) -> dict[str, Any]: ...

    def connect(
        self, action: str, code: str | None = None, state: str | None = None
    ) -> dict[str, Any]: ...

    def list_designs(
        self, query: str | None, limit: int, continuation: str | None
    ) -> dict[str, Any]: ...

    def get_design(self, design_id: str) -> dict[str, Any]: ...

    def create_design(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def edit_design(self, request: dict[str, Any]) -> dict[str, Any]: ...

    def upload_asset(self, path: Path, name: str) -> dict[str, Any]: ...

    def export_design(
        self, design_id: str, format_type: str, destination: Path, quality: str | None = None
    ) -> dict[str, Any]: ...


class TranscriptionProvider(Protocol):
    def capabilities(self) -> dict[str, Any]: ...

    def transcribe(self, audio: Path, language: str | None) -> dict[str, Any]: ...
