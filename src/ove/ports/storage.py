"""Blob storage boundary."""

from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    def ingest(self, source: str) -> tuple[str, str, int]: ...

    def path(self, key: str) -> Path: ...

    def publish(self, temporary: Path) -> tuple[str, str, int]: ...
