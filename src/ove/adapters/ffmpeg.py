"""FFmpeg implementation. Filter graphs are compiled only from typed operations.

No caller can supply a raw filter string: every filter argument is derived from a
validated domain operation, and subprocesses are always invoked with argument
arrays and a protocol/format allowlist.
"""

import itertools
import json
import shutil
import tempfile
from collections.abc import Callable, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

from ove.config import Settings
from ove.domain.errors import OveError
from ove.domain.models import (
    AudioExtractSpec,
    AudioFade,
    AudioInfo,
    AudioMixSpec,
    AudioNormalize,
    AudioProcessSpec,
    AudioReplaceSpec,
    Captions,
    Color,
    ColorGrade,
    ConcatSpec,
    Crop,
    Cut,
    Denoise,
    ExportSpec,
    Flip,
    FrameSpec,
    Freeze,
    ImageInfo,
    Lighting,
    MediaInfo,
    Operation,
    OverlaySpec,
    ProbedMedia,
    ProgressBar,
    Resize,
    Rotate,
    Sharpen,
    Speed,
    Stabilize,
    TextOverlay,
    Title,
    Trim,
    Upscale,
    Zoom,
)
from ove.motion.ass import compose, text_operations
from ove.utilities.process import run_process

#: Demuxers the engine is willing to read. Everything else is rejected outright.
FORMAT_WHITELIST = (
    "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv,image2,mp3,wav,flac,aac,m4a,3gp,asf,image2pipe"
)

IMAGE_FORMATS = {"image2", "image2pipe", "png_pipe", "jpeg_pipe", "webp_pipe", "mjpeg"}

VIDEO_ENCODERS = {"h264": "libx264", "hevc": "libx265", "vp9": "libvpx-vp9"}
AUDIO_ENCODERS = {"aac": "aac", "opus": "libopus", "mp3": "libmp3lame", "vorbis": "libvorbis"}
MUXERS = {"mp4": "mp4", "mov": "mov", "mkv": "matroska", "webm": "webm"}
AUDIO_MUXERS = {"m4a": "ipod", "mp3": "mp3", "wav": "wav", "opus": "ogg", "webm": "webm"}
AUDIO_CONTAINER_CODEC = {
    "m4a": "aac",
    "mp3": "mp3",
    "wav": "pcm_s16le",
    "opus": "opus",
    "webm": "opus",
}

#: Operation type -> FFmpeg filters that must exist for it to be offered.
OPERATION_REQUIREMENTS: dict[str, set[str]] = {
    "trim": {"trim", "atrim", "setpts", "asetpts"},
    "cut": {"trim", "atrim", "setpts", "asetpts", "concat"},
    "resize": {"scale", "pad", "crop", "setsar"},
    "upscale": {"scale", "pad", "crop", "setsar", "unsharp"},
    "crop": {"crop"},
    "rotate": {"transpose", "hflip", "vflip"},
    "flip": {"hflip", "vflip"},
    "speed": {"setpts", "atempo"},
    "freeze": {"trim", "setpts", "tpad", "concat", "apad"},
    "zoom": {"zoompan"},
    "stabilize": {"vidstabdetect", "vidstabtransform"},
    "lighting": {"eq"},
    "color": {"eq"},
    "color_grade": {"eq", "colorbalance", "curves", "colortemperature"},
    "denoise": {"hqdn3d"},
    "sharpen": {"unsharp"},
    "title": {"subtitles"},
    "text_overlay": {"subtitles"},
    "progress_bar": {"drawbox"},
    "captions": {"subtitles"},
}

JOB_REQUIREMENTS: dict[str, set[str]] = {
    "concat": {"concat", "scale", "pad", "setsar"},
    "concat_transition": {"xfade", "acrossfade", "scale", "pad", "setsar"},
    "split": {"trim", "atrim", "setpts", "asetpts"},
    "frame": {"scale"},
    "audio_extract": set(),
    "audio_process": {"loudnorm", "afade", "atrim", "asetpts", "volume"},
    "audio_replace": {"amix", "volume", "apad"},
    "audio_mix": {"amix", "volume"},
    "overlay": {"overlay", "scale", "format", "colorchannelmixer", "fade"},
}

GRADE_LOOKS = {"neutral", "warm", "cool", "cinematic", "vintage", "noir", "vivid", "faded"}


def resize_filter(width: int, height: int, fit: str) -> str:
    if fit == "stretch":
        return f"scale={width}:{height}:flags=lanczos,setsar=1"
    if fit == "crop":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase:"
            f"force_divisible_by=2,crop={width}:{height},setsar=1"
        )
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        f"force_divisible_by=2,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )


def frame_rate_ratio(frame_rate: str) -> Fraction:
    """Parse an ffprobe rational frame rate; fall back to MediaInfo's 25 fps default."""
    try:
        numerator, denominator = (int(part) for part in frame_rate.split("/"))
    except (ValueError, TypeError):
        return Fraction(1, 25)
    if numerator <= 0 or denominator <= 0:
        return Fraction(1, 25)
    return Fraction(numerator, denominator)


def escape_filter_path(path: Path) -> str:
    """Escape a filesystem path for use inside a quoted filter argument."""
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\''")


def kept_intervals(
    ranges: Sequence[tuple[float, float]], duration: float
) -> list[tuple[float, float]]:
    """Invert removal ranges into the retained half-open intervals."""
    kept: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in sorted(ranges):
        if start > cursor + 0.001:
            kept.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration - 0.001:
        kept.append((cursor, duration))
    if not kept:
        raise OveError("invalid_range", "Cutting these ranges would remove the whole timeline.")
    return kept


def grade_filters(look: str, intensity: float) -> list[str]:
    """Named creative looks, interpolated toward neutral by ``intensity``."""
    if look == "neutral" or intensity <= 0:
        return []

    def blend(neutral: float, graded: float) -> float:
        return neutral + (graded - neutral) * intensity

    if look == "warm":
        return [f"colortemperature=temperature={blend(6500, 4300):.0f}"]
    if look == "cool":
        return [f"colortemperature=temperature={blend(6500, 9500):.0f}"]
    if look == "vivid":
        return [f"eq=saturation={blend(1, 1.35):.3f}:contrast={blend(1, 1.08):.3f}"]
    if look == "noir":
        return [f"eq=saturation={blend(1, 0):.3f}:contrast={blend(1, 1.25):.3f}"]
    if look == "faded":
        return [
            f"curves=all='0/{0.12 * intensity:.3f} 1/1'",
            f"eq=saturation={blend(1, 0.85):.3f}",
        ]
    if look == "vintage":
        return [
            f"curves=all='0/{0.08 * intensity:.3f} 1/1'",
            f"eq=saturation={blend(1, 0.78):.3f}",
            f"colorbalance=rs={0.06 * intensity:.3f}:bs={-0.04 * intensity:.3f}",
        ]
    if look == "cinematic":
        return [
            f"colorbalance=rs={-0.05 * intensity:.3f}:bs={0.08 * intensity:.3f}"
            f":gs={0.02 * intensity:.3f}",
            f"eq=contrast={blend(1, 1.15):.3f}:saturation={blend(1, 0.95):.3f}",
        ]
    raise OveError("unsupported_media", f"Unknown colour look: {look}")


class _Graph:
    """Accumulates a ``filter_complex`` expression with explicit stream labels."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.video = "0:v"
        self.audio = "0:a"
        self.video_filters: list[str] = []
        self.audio_filters: list[str] = []
        self._counter = itertools.count(1)

    def label(self, prefix: str) -> str:
        return f"{prefix}{next(self._counter)}"

    def flush(self, *, audio: bool = True, force_video: bool = False) -> None:
        if self.video_filters or force_video:
            target = self.label("v")
            filters = ",".join(self.video_filters) if self.video_filters else "null"
            self.parts.append(f"[{self.video}]{filters}[{target}]")
            self.video = target
            self.video_filters = []
        if audio and self.audio_filters:
            target = self.label("a")
            self.parts.append(f"[{self.audio}]{','.join(self.audio_filters)}[{target}]")
            self.audio = target
            self.audio_filters = []

    def expression(self, inputs: list[str], filters: str, prefix: str) -> str:
        target = self.label(prefix)
        joined = "".join(f"[{item}]" for item in inputs)
        self.parts.append(f"{joined}{filters}[{target}]")
        return target

    @property
    def video_map(self) -> str:
        return f"[{self.video}]" if self.video != "0:v" else "0:v:0"

    @property
    def audio_map(self) -> str:
        return f"[{self.audio}]" if self.audio != "0:a" else "0:a:0"

    def text(self) -> str:
        return ";".join(self.parts)


class FFmpegEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    # ------------------------------------------------------------------ discovery

    def capabilities(self) -> dict[str, object]:
        available = bool(
            shutil.which(self.settings.ffmpeg_path) and shutil.which(self.settings.ffprobe_path)
        )
        if not available:
            return {
                "available": False,
                "reason": "Install FFmpeg and ffprobe or set their paths.",
                "operations": [],
                "jobs": [],
                "encoders": [],
            }
        filters = run_process([self.settings.ffmpeg_path, "-hide_banner", "-filters"], 10)
        encoders = run_process([self.settings.ffmpeg_path, "-hide_banner", "-encoders"], 10)
        names = {line.split()[1] for line in filters.splitlines() if len(line.split()) >= 2}
        encoder_names = {
            line.split()[1] for line in encoders.splitlines() if len(line.split()) >= 2
        }
        wanted = {
            *VIDEO_ENCODERS.values(),
            *AUDIO_ENCODERS.values(),
            "pcm_s16le",
            "mjpeg",
            "png",
            "libwebp",
        }
        return {
            "available": "libx264" in encoder_names,
            "operations": sorted(
                key for key, required in OPERATION_REQUIREMENTS.items() if required <= names
            ),
            "jobs": sorted(key for key, required in JOB_REQUIREMENTS.items() if required <= names),
            "encoders": sorted(encoder_names & wanted),
            "version": run_process([self.settings.ffmpeg_path, "-version"], 10).splitlines()[0],
        }

    def _require(self, kind: str, name: str, available: object) -> None:
        if not isinstance(available, list) or name not in available:
            raise OveError(
                "missing_dependency",
                f"This FFmpeg build does not provide {name}.",
                f"Install an FFmpeg build that supports {name} to use {kind}.",
            )

    def require_operation(self, operation: str) -> None:
        self._require("this operation", operation, self.capabilities().get("operations"))

    def require_job(self, job: str) -> None:
        self._require("this job", job, self.capabilities().get("jobs"))

    # ---------------------------------------------------------------------- probing

    def _ffprobe(self, path: Path) -> dict[str, Any]:
        raw = run_process(
            [
                self.settings.ffprobe_path,
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                FORMAT_WHITELIST,
                "-show_format",
                "-show_streams",
                "-of",
                "json",
                str(path),
            ],
            self.settings.probe_timeout_seconds,
        )
        try:
            data: dict[str, Any] = json.loads(raw)
            data["streams"]
            data.setdefault("format", {})
        except (ValueError, KeyError, TypeError) as exc:
            raise OveError("invalid_media", "ffprobe did not return valid media metadata.") from exc
        return data

    def _video_info(self, data: dict[str, Any], video: dict[str, Any]) -> MediaInfo:
        streams = data["streams"]
        audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
        videos = [stream for stream in streams if stream.get("codec_type") == "video"]
        audio = audios[0] if audios else {}
        rotation = int(video.get("tags", {}).get("rotate", 0))
        for side in video.get("side_data_list", []):
            rotation = int(side.get("rotation", rotation))
        try:
            info = MediaInfo(
                duration=float(data["format"].get("duration", video.get("duration", 0))),
                width=video["width"],
                height=video["height"],
                video_codec=video["codec_name"],
                pixel_format=video.get("pix_fmt", "unknown"),
                frame_rate=video.get("avg_frame_rate", "0/0"),
                audio_codec=audio.get("codec_name"),
                audio_channels=audio.get("channels"),
                audio_sample_rate=int(audio["sample_rate"]) if audio.get("sample_rate") else None,
                color_transfer=video.get("color_transfer"),
                color_space=video.get("color_space"),
                color_primaries=video.get("color_primaries"),
                color_range=video.get("color_range"),
                rotation=rotation % 360,
                sample_aspect_ratio=video.get("sample_aspect_ratio", "1:1"),
                video_streams=len(videos),
                audio_streams=len(audios),
                other_streams=len(streams) - len(videos) - len(audios),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise OveError("invalid_media", "ffprobe returned incomplete video metadata.") from exc
        if info.width * info.height > self.settings.max_pixels:
            raise OveError("resource_limit", "Source exceeds OVE_MAX_PIXELS.")
        if info.duration > self.settings.max_duration_seconds:
            raise OveError("resource_limit", "Source exceeds OVE_MAX_DURATION_SECONDS.")
        return info

    def inspect(self, path: Path) -> ProbedMedia:
        """Detect the asset kind and return the matching probe block."""
        data = self._ffprobe(path)
        videos = [stream for stream in data["streams"] if stream.get("codec_type") == "video"]
        audios = [stream for stream in data["streams"] if stream.get("codec_type") == "audio"]
        format_name = str(data["format"].get("format_name", "")).split(",")[0]
        if not videos:
            if not audios:
                raise OveError("unsupported_media", "No audio or video stream was found.")
            return ProbedMedia(kind="audio", audio=self.probe_audio(path))
        if not audios and format_name in IMAGE_FORMATS:
            return ProbedMedia(kind="image", image=self.probe_image(path))
        return ProbedMedia(kind="video", video=self._video_info(data, videos[0]))

    def probe(self, path: Path) -> MediaInfo:
        """Probe a video asset. Images and audio-only files are rejected."""
        data = self._ffprobe(path)
        videos = [stream for stream in data["streams"] if stream.get("codec_type") == "video"]
        if not videos:
            raise OveError("unsupported_media", "A video stream is required.")
        if str(data["format"].get("format_name", "")).split(",")[0] in IMAGE_FORMATS:
            raise OveError("unsupported_media", "This asset is a still image, not a video.")
        return self._video_info(data, videos[0])

    def probe_audio(self, path: Path) -> AudioInfo:
        data = self._ffprobe(path)
        audios = [stream for stream in data["streams"] if stream.get("codec_type") == "audio"]
        if not audios:
            raise OveError("no_audio", "This asset has no audio stream.")
        audio = audios[0]
        try:
            return AudioInfo(
                duration=float(data["format"].get("duration", audio.get("duration", 0)) or 0),
                audio_codec=audio["codec_name"],
                channels=int(audio.get("channels", 1)),
                sample_rate=int(audio.get("sample_rate", 48000)),
                bit_rate=int(audio["bit_rate"]) if audio.get("bit_rate") else None,
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise OveError("invalid_media", "ffprobe returned incomplete audio metadata.") from exc

    def probe_image(self, path: Path) -> ImageInfo:
        data = self._ffprobe(path)
        videos = [stream for stream in data["streams"] if stream.get("codec_type") == "video"]
        if not videos:
            raise OveError("unsupported_media", "The overlay asset must be an image or a video.")
        stream = videos[0]
        return ImageInfo(
            width=int(stream["width"]), height=int(stream["height"]), codec=stream["codec_name"]
        )

    # --------------------------------------------------------------- output codecs

    def _video_encoder_args(self, export: ExportSpec) -> list[str]:
        if export.video_codec == "copy":
            return ["-c:v", "copy"]
        encoder = VIDEO_ENCODERS[export.video_codec]
        arguments = ["-c:v", encoder, "-crf", str(export.crf)]
        if export.video_codec == "vp9":
            arguments += ["-b:v", "0", "-row-mt", "1"]
        else:
            arguments += ["-preset", export.encoder_preset]
        if export.video_codec == "hevc" and export.container in {"mp4", "mov"}:
            arguments += ["-tag:v", "hvc1"]
        return arguments + ["-pix_fmt", "yuv420p"]

    def _audio_encoder_args(self, export: ExportSpec) -> list[str]:
        if export.audio == "drop":
            return ["-an"]
        if export.audio == "copy":
            return ["-c:a", "copy"]
        return ["-c:a", AUDIO_ENCODERS[export.audio], "-b:a", f"{export.audio_bitrate_kbps}k"]

    def _container_args(self, export: ExportSpec) -> list[str]:
        arguments = ["-f", MUXERS[export.container]]
        if export.container in {"mp4", "mov"}:
            arguments = ["-movflags", "+faststart", *arguments]
        return arguments

    def _source_args(self) -> list[str]:
        return [
            "-hide_banner",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-y",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            FORMAT_WHITELIST,
            "-threads",
            str(self.settings.ffmpeg_threads),
            "-noautorotate",
        ]

    # ------------------------------------------------------------------- rendering

    def _zoom_filter(
        self, operation: Zoom, width: int, height: int, fps: Fraction, duration: float
    ) -> str:
        frames = max(2, round(duration * float(fps)))
        span = max(0.0, operation.end_zoom - operation.start_zoom)
        zoom = f"{operation.start_zoom}+{span:.4f}*on/{frames - 1}"
        x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
        if operation.focus == "top":
            y = "0"
        elif operation.focus == "bottom":
            y = "ih-(ih/zoom)"
        elif operation.focus == "left":
            x = "0"
        elif operation.focus == "right":
            x = "iw-(iw/zoom)"
        return (
            f"zoompan=z='min({zoom},{operation.end_zoom})':x='{x}':y='{y}'"
            f":d=1:s={width}x{height}:fps={fps.numerator}/{fps.denominator}"
        )

    @staticmethod
    def _progress_filter(operation: ProgressBar, height: int, duration: float) -> str:
        thickness = min(operation.thickness, height)
        y = (
            operation.margin
            if operation.position == "top"
            else height - thickness - operation.margin
        )
        span = max(duration, 0.001)
        return (
            f"drawbox=x=0:y={max(0, y)}:w='iw*min(t/{span:.4f},1)'"
            f":h={thickness}:color={operation.color}@0.95:t=fill"
        )

    def _stabilize_pass(
        self, source: Path, operation: Stabilize, cancelled: Callable[[], bool], scratch: Path
    ) -> str:
        transforms = scratch / "transforms.trf"
        run_process(
            [
                self.settings.ffmpeg_path,
                *self._source_args(),
                "-i",
                str(source),
                "-vf",
                f"vidstabdetect=shakiness=5:accuracy=15:result={escape_filter_path(transforms)}",
                "-f",
                "null",
                "-",
            ],
            self.settings.job_timeout_seconds,
            cancelled,
        )
        if not transforms.is_file():
            raise OveError(
                "engine_failed",
                "Stabilization analysis produced no transform data.",
                "Confirm the source has enough visual detail to track.",
            )
        return (
            f"vidstabtransform=input={escape_filter_path(transforms)}"
            f":smoothing={operation.smoothing}:crop={operation.crop}"
            f":optzoom={1 if operation.crop == 'black' else 0}"
            f":zoom={operation.zoom}:interpol=bicubic"
        )

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
    ) -> None:
        has_audio = bool(info.audio_codec) and export.audio != "drop"
        duration = expected_duration if expected_duration is not None else info.duration
        width, height = info.width, info.height
        fps = frame_rate_ratio(info.frame_rate)
        graph = _Graph()
        rate: Fraction | None = None

        with tempfile.TemporaryDirectory(prefix="ove-render-") as directory:
            scratch = Path(directory)
            for operation in operations:
                if isinstance(operation, Title | TextOverlay | Captions | ProgressBar):
                    continue
                if isinstance(operation, Cut):
                    graph.flush(audio=has_audio)
                    intervals = kept_intervals(
                        [(item.start, item.end) for item in operation.ranges], duration
                    )
                    video_inputs, audio_inputs = [], []
                    for start, end in intervals:
                        video_inputs.append(
                            graph.expression(
                                [graph.video],
                                f"trim=start={start}:end={end},setpts=PTS-STARTPTS",
                                "cv",
                            )
                        )
                        if has_audio:
                            audio_inputs.append(
                                graph.expression(
                                    [graph.audio],
                                    f"atrim=start={start}:end={end},asetpts=PTS-STARTPTS",
                                    "ca",
                                )
                            )
                    joined = "".join(f"[{item}]" for item in video_inputs + audio_inputs)
                    target_v = graph.label("v")
                    if has_audio:
                        target_a = graph.label("a")
                        graph.parts.append(
                            f"{joined}concat=n={len(video_inputs)}:v=1:a=1[{target_v}][{target_a}]"
                        )
                        graph.audio = target_a
                    else:
                        graph.parts.append(
                            f"{joined}concat=n={len(video_inputs)}:v=1:a=0[{target_v}]"
                        )
                    graph.video = target_v
                elif isinstance(operation, Freeze):
                    graph.flush(audio=has_audio)
                    head_v = graph.expression(
                        [graph.video],
                        f"trim=start=0:end={operation.at},setpts=PTS-STARTPTS",
                        "fv",
                    )
                    tail_v = graph.expression(
                        [graph.video],
                        f"trim=start={operation.at},setpts=PTS-STARTPTS,"
                        f"tpad=start_duration={operation.duration}:start_mode=clone",
                        "fv",
                    )
                    target_v = graph.label("v")
                    if has_audio:
                        head_a = graph.expression(
                            [graph.audio],
                            f"atrim=start=0:end={operation.at},asetpts=PTS-STARTPTS",
                            "fa",
                        )
                        tail_a = graph.expression(
                            [graph.audio],
                            f"atrim=start={operation.at},asetpts=PTS-STARTPTS,"
                            f"apad=pad_dur={operation.duration}",
                            "fa",
                        )
                        target_a = graph.label("a")
                        graph.parts.append(
                            f"[{head_v}][{head_a}][{tail_v}][{tail_a}]"
                            f"concat=n=2:v=1:a=1[{target_v}][{target_a}]"
                        )
                        graph.audio = target_a
                    else:
                        graph.parts.append(f"[{head_v}][{tail_v}]concat=n=2:v=1:a=0[{target_v}]")
                    graph.video = target_v
                elif isinstance(operation, Trim):
                    graph.video_filters += [
                        f"trim=start={operation.start}:end={operation.end}",
                        "setpts=PTS-STARTPTS",
                    ]
                    if has_audio:
                        graph.audio_filters += [
                            f"atrim=start={operation.start}:end={operation.end}",
                            "asetpts=PTS-STARTPTS",
                        ]
                elif isinstance(operation, Stabilize):
                    graph.video_filters.append(
                        self._stabilize_pass(source, operation, cancelled, scratch)
                    )
                elif isinstance(operation, Resize | Upscale):
                    graph.video_filters.append(
                        resize_filter(operation.width, operation.height, operation.fit)
                    )
                    if isinstance(operation, Upscale) and operation.sharpen > 0:
                        graph.video_filters.append(f"unsharp=5:5:{operation.sharpen}:5:5:0")
                    width, height = operation.width, operation.height
                elif isinstance(operation, Crop):
                    graph.video_filters.append(
                        f"crop={operation.width}:{operation.height}:{operation.x}:{operation.y}"
                    )
                    width, height = operation.width, operation.height
                elif isinstance(operation, Rotate):
                    graph.video_filters.append(
                        {90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}[
                            operation.degrees
                        ]
                    )
                    if operation.degrees in {90, 270}:
                        width, height = height, width
                elif isinstance(operation, Flip):
                    graph.video_filters.append(
                        "hflip" if operation.axis == "horizontal" else "vflip"
                    )
                elif isinstance(operation, Speed):
                    graph.video_filters.append(f"setpts=(PTS-STARTPTS)/{operation.factor}")
                    rate = (rate or fps) * Fraction(operation.factor).limit_denominator(1000)
                    factor = operation.factor
                    if has_audio:
                        while factor < 0.5:
                            graph.audio_filters.append("atempo=0.5")
                            factor /= 0.5
                        while factor > 2:
                            graph.audio_filters.append("atempo=2")
                            factor /= 2
                        graph.audio_filters.append(f"atempo={factor}")
                elif isinstance(operation, Zoom):
                    graph.video_filters.append(
                        self._zoom_filter(operation, width, height, fps, duration)
                    )
                elif isinstance(operation, Lighting):
                    graph.video_filters.append(
                        f"eq=brightness={operation.brightness}:contrast={operation.contrast}"
                        f":gamma={operation.gamma}"
                    )
                elif isinstance(operation, Color):
                    graph.video_filters.append(f"eq=saturation={operation.saturation}")
                elif isinstance(operation, ColorGrade):
                    graph.video_filters += grade_filters(operation.look, operation.intensity)
                elif isinstance(operation, Denoise):
                    graph.video_filters.append(f"hqdn3d={operation.strength}")
                elif isinstance(operation, Sharpen):
                    graph.video_filters.append(f"unsharp=5:5:{operation.amount}:5:5:0")
                else:  # pragma: no cover - the discriminated union is exhaustive
                    raise OveError(
                        "unsupported_operation", f"Unhandled operation: {operation.type}"
                    )

            graph.audio_filters += audio_filters or []
            graph.flush(audio=has_audio)

            overlay_filters: list[str] = []
            for bar in [item for item in operations if isinstance(item, ProgressBar)]:
                overlay_filters.append(self._progress_filter(bar, height, duration))
            if text_operations(operations):
                subtitles = scratch / "overlay.ass"
                subtitles.write_text(compose(operations, width, height))
                overlay_filters.append(f"subtitles=filename='{escape_filter_path(subtitles)}'")
            if export.width is not None and export.height is not None:
                width, height = export.width, export.height
                overlay_filters.append(resize_filter(width, height, export.fit))
            if rate is not None:
                overlay_filters.append(f"fps={rate.numerator}/{rate.denominator}")
            graph.video_filters = overlay_filters
            graph.flush(audio=has_audio, force_video=True)

            arguments = [self.settings.ffmpeg_path, *self._source_args(), "-i", str(source)]
            arguments += ["-filter_complex", graph.text(), "-map", graph.video_map]
            if has_audio:
                arguments += ["-map", graph.audio_map]
            arguments += self._video_encoder_args(export)
            arguments += self._audio_encoder_args(export) if has_audio else ["-an"]
            if has_audio and export.audio not in {"drop", "copy"}:
                arguments += ["-ar", str(info.audio_sample_rate or 48000)]
            arguments += ["-fps_mode", "passthrough", "-threads", str(self.settings.ffmpeg_threads)]
            for field, option in [
                (info.color_transfer, "-color_trc"),
                (info.color_primaries, "-color_primaries"),
                (info.color_space, "-colorspace"),
                (info.color_range, "-color_range"),
            ]:
                if field and field not in {"unknown", "unspecified"}:
                    arguments += [option, field]
            arguments += self._container_args(export)
            arguments.append(str(output))
            run_process(
                arguments,
                self.settings.job_timeout_seconds,
                cancelled,
                output,
                self.settings.max_output_bytes,
            )

    # ------------------------------------------------------------------ multi-input

    def render_concat(
        self,
        sources: list[Path],
        infos: list[MediaInfo],
        output: Path,
        spec: ConcatSpec,
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> None:
        if spec.transition != "none":
            self.require_job("concat_transition")
        self.require_job("concat")
        target_width = export.width or infos[0].width
        target_height = export.height or infos[0].height
        has_audio = any(bool(info.audio_codec) for info in infos) and export.audio != "drop"
        parts: list[str] = []
        video_labels: list[str] = []
        audio_labels: list[str] = []
        for index, info in enumerate(infos):
            parts.append(
                f"[{index}:v]scale={target_width}:{target_height}:"
                f"force_original_aspect_ratio=decrease:force_divisible_by=2,"
                f"pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
                f"format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[c{index}v]"
            )
            video_labels.append(f"c{index}v")
            if has_audio:
                if info.audio_codec:
                    parts.append(
                        f"[{index}:a]aformat=sample_fmts=fltp:sample_rates=48000:"
                        f"channel_layouts=stereo,asetpts=PTS-STARTPTS[c{index}a]"
                    )
                else:
                    parts.append(
                        f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                        f"atrim=0:{info.duration:.3f},asetpts=PTS-STARTPTS[c{index}a]"
                    )
                audio_labels.append(f"c{index}a")

        if spec.transition == "none":
            joined = "".join(f"[{item}]" for item in video_labels + audio_labels)
            if has_audio:
                parts.append(f"{joined}concat=n={len(infos)}:v=1:a=1[vout][aout]")
            else:
                parts.append(f"{joined}concat=n={len(infos)}:v=1:a=0[vout]")
        else:
            self._xfade_chain(parts, video_labels, audio_labels, infos, spec, has_audio)

        arguments = [self.settings.ffmpeg_path, *self._source_args()]
        for path in sources:
            arguments += ["-i", str(path)]
        arguments += ["-filter_complex", ";".join(parts), "-map", "[vout]"]
        if has_audio:
            arguments += ["-map", "[aout]"]
        arguments += self._video_encoder_args(export)
        arguments += self._audio_encoder_args(export) if has_audio else ["-an"]
        arguments += ["-fps_mode", "cfr", "-threads", str(self.settings.ffmpeg_threads)]
        arguments += self._container_args(export)
        arguments.append(str(output))
        run_process(
            arguments,
            self.settings.job_timeout_seconds,
            cancelled,
            output,
            self.settings.max_output_bytes,
        )

    def _xfade_chain(
        self,
        parts: list[str],
        video_labels: list[str],
        audio_labels: list[str],
        infos: list[MediaInfo],
        spec: ConcatSpec,
        has_audio: bool,
    ) -> None:
        transition = "fade" if spec.transition == "dissolve" else spec.transition
        duration = spec.transition_duration
        current_v = video_labels[0]
        current_a = audio_labels[0] if has_audio else None
        offset = infos[0].duration - duration
        for index in range(1, len(video_labels)):
            next_v = f"xv{index}"
            parts.append(
                f"[{current_v}][{video_labels[index]}]"
                f"xfade=transition={transition}:duration={duration}:offset={max(0.0, offset):.4f}"
                f"[{next_v}]"
            )
            current_v = next_v
            if current_a is not None:
                next_a = f"xa{index}"
                parts.append(
                    f"[{current_a}][{audio_labels[index]}]acrossfade=d={duration}[{next_a}]"
                )
                current_a = next_a
            offset += infos[index].duration - duration
        parts.append(f"[{current_v}]null[vout]")
        if current_a is not None:
            parts.append(f"[{current_a}]anull[aout]")

    def split_video(
        self,
        source: Path,
        output_dir: Path,
        info: MediaInfo,
        boundaries: list[float],
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> list[Path]:
        self.require_job("split")
        bounds = [0.0, *boundaries, info.duration]
        outputs: list[Path] = []
        for index, (start, end) in enumerate(zip(bounds, bounds[1:], strict=False), start=1):
            target = output_dir / f"segment-{index:03d}.{export.container}"
            self.render(
                source,
                target,
                info,
                [Trim(start=start, end=end)],
                export,
                cancelled,
                expected_duration=end - start,
            )
            outputs.append(target)
        return outputs

    def extract_frame(
        self, source: Path, output: Path, spec: FrameSpec, cancelled: Callable[[], bool]
    ) -> None:
        self.require_job("frame")
        arguments = [
            self.settings.ffmpeg_path,
            *self._source_args(),
            "-ss",
            f"{spec.at:.3f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
        ]
        if spec.width:
            arguments += ["-vf", f"scale={spec.width}:-2:flags=lanczos"]
        if spec.format == "jpg":
            arguments += ["-c:v", "mjpeg", "-q:v", str(spec.quality)]
        elif spec.format == "webp":
            arguments += ["-c:v", "libwebp", "-quality", str(max(1, 100 - spec.quality * 3))]
        else:
            arguments += ["-c:v", "png"]
        arguments += ["-f", "image2", str(output)]
        run_process(arguments, self.settings.probe_timeout_seconds, cancelled, output, 200_000_000)

    @staticmethod
    def _audio_codec_args(container: str, bitrate_kbps: int) -> list[str]:
        codec = AUDIO_CONTAINER_CODEC[container]
        arguments = ["-c:a", codec]
        if codec != "pcm_s16le":
            arguments += ["-b:a", f"{bitrate_kbps}k"]
        return arguments

    def extract_audio(
        self, source: Path, output: Path, spec: AudioExtractSpec, cancelled: Callable[[], bool]
    ) -> None:
        self.require_job("audio_extract")
        arguments = [
            self.settings.ffmpeg_path,
            *self._source_args(),
            "-i",
            str(source),
            "-map",
            "0:a:0",
            "-vn",
        ]
        arguments += self._audio_codec_args(spec.container, spec.bitrate_kbps)
        if spec.sample_rate:
            arguments += ["-ar", str(spec.sample_rate)]
        if spec.channels:
            arguments += ["-ac", str(spec.channels)]
        arguments += ["-f", AUDIO_MUXERS[spec.container], str(output)]
        run_process(
            arguments,
            self.settings.job_timeout_seconds,
            cancelled,
            output,
            self.settings.max_output_bytes,
        )

    @staticmethod
    def _normalize_filter(spec: AudioNormalize) -> str:
        return f"loudnorm=I={spec.target_lufs}:TP={spec.true_peak_db}:LRA={spec.loudness_range}"

    @staticmethod
    def _fade_filters(spec: AudioFade, duration: float) -> list[str]:
        filters: list[str] = []
        if spec.fade_in > 0:
            filters.append(f"afade=t=in:st=0:d={spec.fade_in}:curve={spec.curve}")
        if spec.fade_out > 0:
            start = max(0.0, duration - spec.fade_out)
            filters.append(f"afade=t=out:st={start:.3f}:d={spec.fade_out}:curve={spec.curve}")
        return filters

    def process_audio(
        self,
        source: Path,
        output: Path,
        spec: AudioProcessSpec,
        media: ProbedMedia,
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> None:
        """Apply audio operations, preserving the source's video when present."""
        self.require_job("audio_process")
        duration = media.duration
        trim = (spec.trim.start, spec.trim.end) if spec.trim else None
        if trim:
            duration = trim[1] - trim[0]
        filters: list[str] = []
        if spec.normalize is not None:
            filters.append(self._normalize_filter(spec.normalize))
        if spec.fade is not None:
            filters += self._fade_filters(spec.fade, duration)

        if media.video is not None:
            operations: list[Operation] = []
            if trim:
                operations.append(Trim(start=trim[0], end=trim[1]))
            self.render(
                source,
                output,
                media.video,
                operations,
                export.model_copy(update={"audio": "drop" if spec.remove else "aac"}),
                cancelled,
                expected_duration=duration,
                audio_filters=filters,
            )
            return

        if spec.remove:
            raise OveError(
                "unsupported_media",
                "Removing the only audio stream would produce an empty file.",
                "Extract from a different asset, or delete this asset instead.",
            )
        audio = media.audio
        assert audio is not None
        chain = filters or [f"aformat=sample_rates={audio.sample_rate}"]
        if trim:
            chain = [f"atrim=start={trim[0]}:end={trim[1]}", "asetpts=PTS-STARTPTS", *chain]
        container = spec.container if spec.container != "keep" else "m4a"
        arguments = [
            self.settings.ffmpeg_path,
            *self._source_args(),
            "-i",
            str(source),
            "-vn",
            "-af",
            ",".join(chain),
        ]
        arguments += self._audio_codec_args(container, export.audio_bitrate_kbps)
        arguments += ["-f", AUDIO_MUXERS[container], str(output)]
        run_process(
            arguments,
            self.settings.job_timeout_seconds,
            cancelled,
            output,
            self.settings.max_output_bytes,
        )

    def replace_audio(
        self,
        video: Path,
        replacement: Path,
        output: Path,
        spec: AudioReplaceSpec,
        media: ProbedMedia,
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> None:
        self.require_job("audio_replace")
        assert media.video is not None
        duration = media.video.duration
        if spec.mix_with_original > 0:
            graph = (
                f"[0:a]volume=1[a0];[1:a]volume={spec.mix_with_original}[a1];"
                f"[a0][a1]amix=inputs=2:duration=first:dropout_transition=0,"
                f"apad,atrim=0:{duration:.3f}[aout]"
            )
        else:
            graph = f"[1:a]apad,atrim=0:{duration:.3f}[aout]"
        arguments = [self.settings.ffmpeg_path, *self._source_args()]
        if spec.loop:
            arguments += ["-stream_loop", "-1"]
        arguments += ["-i", str(video), "-i", str(replacement)]
        arguments += ["-filter_complex", graph, "-map", "0:v:0", "-map", "[aout]"]
        arguments += self._video_encoder_args(export)
        arguments += ["-c:a", AUDIO_ENCODERS["aac"], "-b:a", f"{export.audio_bitrate_kbps}k"]
        arguments += ["-threads", str(self.settings.ffmpeg_threads)]
        arguments += self._container_args(export)
        arguments.append(str(output))
        run_process(
            arguments,
            self.settings.job_timeout_seconds,
            cancelled,
            output,
            self.settings.max_output_bytes,
        )

    def mix_audio(
        self,
        sources: list[Path],
        output: Path,
        spec: AudioMixSpec,
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> None:
        """Mix ``sources``; when ``spec.video_asset_id`` is set the last input carries video."""
        self.require_job("audio_mix")
        weights = spec.weights
        parts: list[str] = []
        for position in range(len(sources)):
            gain = weights[position] if weights else 1.0 / max(len(sources), 1)
            parts.append(f"[{position}:a]volume={gain:.4f}[m{position}]")
        joined = "".join(f"[m{position}]" for position in range(len(sources)))
        parts.append(
            f"{joined}amix=inputs={len(sources)}:duration={spec.duration}"
            f":dropout_transition=0[mixed]"
        )
        arguments = [self.settings.ffmpeg_path, *self._source_args()]
        for path in sources:
            arguments += ["-i", str(path)]
        if spec.video_asset_id is not None:
            video_index = len(sources) - 1
            parts.append("[mixed]aformat=sample_rates=48000:channel_layouts=stereo[aout]")
            arguments += [
                "-filter_complex",
                ";".join(parts),
                "-map",
                f"{video_index}:v:0",
                "-map",
                "[aout]",
            ]
            arguments += self._video_encoder_args(export)
            arguments += ["-c:a", AUDIO_ENCODERS["aac"], "-b:a", f"{export.audio_bitrate_kbps}k"]
            arguments += self._container_args(export)
        else:
            arguments += ["-filter_complex", ";".join(parts), "-map", "[mixed]", "-vn"]
            arguments += self._audio_codec_args(spec.container, spec.bitrate_kbps)
            arguments += ["-f", AUDIO_MUXERS[spec.container]]
        arguments += ["-threads", str(self.settings.ffmpeg_threads), str(output)]
        run_process(
            arguments,
            self.settings.job_timeout_seconds,
            cancelled,
            output,
            self.settings.max_output_bytes,
        )

    def render_overlay(
        self,
        base: Path,
        overlay: Path,
        output: Path,
        spec: OverlaySpec,
        info: MediaInfo,
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> None:
        self.require_job("overlay")
        target_width = export.width or info.width
        target_height = export.height or info.height
        logo_width = max(2, int(target_width * spec.scale) // 2 * 2)
        positions = {
            "center": ("(W-w)/2", "(H-h)/2"),
            "top": ("(W-w)/2", f"{spec.margin}"),
            "bottom": ("(W-w)/2", f"H-h-{spec.margin}"),
            "top_left": (f"{spec.margin}", f"{spec.margin}"),
            "top_right": (f"W-w-{spec.margin}", f"{spec.margin}"),
            "bottom_left": (f"{spec.margin}", f"H-h-{spec.margin}"),
            "bottom_right": (f"W-w-{spec.margin}", f"H-h-{spec.margin}"),
        }
        x, y = positions[spec.position]
        end = spec.end if spec.end is not None else info.duration
        logo_filters = [
            f"scale={logo_width}:-2:flags=lanczos",
            "format=rgba",
            f"colorchannelmixer=aa={spec.opacity}",
        ]
        if spec.animation == "fade":
            logo_filters.append(f"fade=t=in:st={spec.start}:d=0.4:alpha=1")
        elif spec.animation == "slide_in":
            travel = int(target_width * 0.12)
            settle = spec.start + 0.4
            x = f"if(lt(t,{settle:.3f}),({x})+{travel}*({settle:.3f}-t)/0.4,({x}))"
        parts = [
            f"[0:v]scale={target_width}:{target_height}:force_original_aspect_ratio=decrease"
            f":force_divisible_by=2,pad={target_width}:{target_height}:(ow-iw)/2:(oh-ih)/2"
            f",setsar=1[vbase]",
            f"[1:v]{','.join(logo_filters)}[vlogo]",
            f"[vbase][vlogo]overlay=x='{x}':y='{y}'"
            f":enable='between(t,{spec.start},{end})':eof_action=pass[vout]",
        ]
        has_audio = bool(info.audio_codec) and export.audio != "drop"
        arguments = [
            self.settings.ffmpeg_path,
            *self._source_args(),
            "-i",
            str(base),
            "-i",
            str(overlay),
            "-filter_complex",
            ";".join(parts),
            "-map",
            "[vout]",
        ]
        if has_audio:
            arguments += ["-map", "0:a:0"]
        arguments += self._video_encoder_args(export)
        arguments += self._audio_encoder_args(export) if has_audio else ["-an"]
        arguments += ["-fps_mode", "cfr", "-threads", str(self.settings.ffmpeg_threads)]
        arguments += self._container_args(export)
        arguments.append(str(output))
        run_process(
            arguments,
            self.settings.job_timeout_seconds,
            cancelled,
            output,
            self.settings.max_output_bytes,
        )

    # ------------------------------------------------------------------- validation

    def decode(self, path: Path, cancelled: Callable[[], bool]) -> None:
        run_process(
            [
                self.settings.ffmpeg_path,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-protocol_whitelist",
                "file",
                "-threads",
                str(self.settings.ffmpeg_threads),
                "-format_whitelist",
                FORMAT_WHITELIST,
                "-i",
                str(path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-f",
                "null",
                "-",
            ],
            self.settings.job_timeout_seconds,
            cancelled,
        )

    def decode_audio(self, path: Path, cancelled: Callable[[], bool]) -> None:
        run_process(
            [
                self.settings.ffmpeg_path,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                FORMAT_WHITELIST,
                "-i",
                str(path),
                "-vn",
                "-f",
                "null",
                "-",
            ],
            self.settings.job_timeout_seconds,
            cancelled,
        )

    def audio_hash(self, path: Path, cancelled: Callable[[], bool]) -> str:
        return run_process(
            [
                self.settings.ffmpeg_path,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                FORMAT_WHITELIST,
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-c:a",
                "copy",
                "-f",
                "hash",
                "-",
            ],
            self.settings.job_timeout_seconds,
            cancelled,
        ).strip()
