"""Replaceable engine contract; paths refer only to staged, owned media."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ove.domain.models import ExportSpec, MediaInfo, Operation


class MediaEngine(Protocol):
    def capabilities(self) -> dict[str, object]: ...

    def probe(self, path: Path) -> MediaInfo: ...

    def render(
        self,
        source: Path,
        output: Path,
        info: MediaInfo,
        operations: list[Operation],
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> None: ...

    def decode(self, path: Path, cancelled: Callable[[], bool]) -> None: ...

    def audio_hash(self, path: Path, cancelled: Callable[[], bool]) -> str: ...
