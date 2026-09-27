"""Replaceable engine contract; paths refer only to staged, owned media."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from ove.domain.models import (
    AudioExtractSpec,
    AudioInfo,
    AudioMixSpec,
    AudioProcessSpec,
    AudioReplaceSpec,
    ConcatSpec,
    ExportSpec,
    FrameSpec,
    ImageInfo,
    MediaInfo,
    Operation,
    OverlaySpec,
    ProbedMedia,
)


class MediaEngine(Protocol):
    def capabilities(self) -> dict[str, object]: ...

    def require_operation(self, operation: str) -> None: ...

    def require_job(self, job: str) -> None: ...

    def inspect(self, path: Path) -> ProbedMedia: ...

    def probe(self, path: Path) -> MediaInfo: ...

    def probe_audio(self, path: Path) -> AudioInfo: ...

    def probe_image(self, path: Path) -> ImageInfo: ...

    def render(
        self,
        source: Path,
        output: Path,
        info: MediaInfo,
        operations: list[Operation],
        export: ExportSpec,
        cancelled: Callable[[], bool],
        expected_duration: float | None = None,
        audio_filters: list[str] | None = None,
    ) -> None: ...

    def render_concat(
        self,
        sources: list[Path],
        infos: list[MediaInfo],
        output: Path,
        spec: ConcatSpec,
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> None: ...

    def split_video(
        self,
        source: Path,
        output_dir: Path,
        info: MediaInfo,
        boundaries: list[float],
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> list[Path]: ...

    def extract_frame(
        self, source: Path, output: Path, spec: FrameSpec, cancelled: Callable[[], bool]
    ) -> None: ...

    def extract_audio(
        self, source: Path, output: Path, spec: AudioExtractSpec, cancelled: Callable[[], bool]
    ) -> None: ...

    def process_audio(
        self,
        source: Path,
        output: Path,
        spec: AudioProcessSpec,
        media: ProbedMedia,
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> None: ...

    def replace_audio(
        self,
        video: Path,
        replacement: Path,
        output: Path,
        spec: AudioReplaceSpec,
        media: ProbedMedia,
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> None: ...

    def mix_audio(
        self,
        sources: list[Path],
        output: Path,
        spec: AudioMixSpec,
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> None: ...

    def render_overlay(
        self,
        base: Path,
        overlay: Path,
        output: Path,
        spec: OverlaySpec,
        info: MediaInfo,
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> None: ...

    def decode(self, path: Path, cancelled: Callable[[], bool]) -> None: ...

    def decode_audio(self, path: Path, cancelled: Callable[[], bool]) -> None: ...

    def audio_hash(self, path: Path, cancelled: Callable[[], bool]) -> str: ...
