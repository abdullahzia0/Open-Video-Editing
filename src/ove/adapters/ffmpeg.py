"""FFmpeg implementation. Filter graphs are compiled only from typed operations."""

import json
import shutil
import tempfile
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

from ove.config import Settings
from ove.domain.errors import OveError
from ove.domain.models import (
    Captions,
    Color,
    Crop,
    Denoise,
    ExportSpec,
    Flip,
    Lighting,
    MediaInfo,
    Operation,
    Resize,
    Rotate,
    Sharpen,
    Speed,
    Title,
    Trim,
)
from ove.motion.ass import compose
from ove.utilities.process import run_process


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


class FFmpegEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def capabilities(self) -> dict[str, object]:
        available = bool(
            shutil.which(self.settings.ffmpeg_path) and shutil.which(self.settings.ffprobe_path)
        )
        if not available:
            return {
                "available": False,
                "reason": "Install FFmpeg and ffprobe or set their paths.",
                "operations": [],
            }
        filters = run_process([self.settings.ffmpeg_path, "-hide_banner", "-filters"], 10)
        encoders = run_process([self.settings.ffmpeg_path, "-hide_banner", "-encoders"], 10)
        names = {line.split()[1] for line in filters.splitlines() if len(line.split()) >= 2}
        encoder_names = {
            line.split()[1] for line in encoders.splitlines() if len(line.split()) >= 2
        }
        requirements = {
            "trim": {"trim", "atrim", "setpts", "asetpts"},
            "resize": {"scale", "pad", "crop", "setsar"},
            "crop": {"crop"},
            "rotate": {"transpose", "hflip", "vflip"},
            "flip": {"hflip", "vflip"},
            "speed": {"setpts", "atempo"},
            "lighting": {"eq"},
            "color": {"eq"},
            "denoise": {"hqdn3d"},
            "sharpen": {"unsharp"},
            "title": {"subtitles"},
            "captions": {"subtitles"},
        }
        operations = [key for key, required in requirements.items() if required <= names]
        return {
            "available": "libx264" in encoder_names,
            "operations": operations,
            "encoders": sorted(encoder_names & {"libx264", "aac"}),
            "version": run_process([self.settings.ffmpeg_path, "-version"], 10).splitlines()[0],
        }

    def probe(self, path: Path) -> MediaInfo:
        raw = run_process(
            [
                self.settings.ffprobe_path,
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-format_whitelist",
                "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv",
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
            videos = [s for s in data["streams"] if s.get("codec_type") == "video"]
            audios = [s for s in data["streams"] if s.get("codec_type") == "audio"]
            if not videos:
                raise OveError("unsupported_media", "A video stream is required.")
            video, audio = videos[0], audios[0] if audios else {}
            rotation = int(video.get("tags", {}).get("rotate", 0))
            for side in video.get("side_data_list", []):
                rotation = int(side.get("rotation", rotation))
            info = MediaInfo(
                duration=float(data.get("format", {}).get("duration", video.get("duration", 0))),
                width=video["width"],
                height=video["height"],
                video_codec=video["codec_name"],
                pixel_format=video.get("pix_fmt", "unknown"),
                frame_rate=video.get("avg_frame_rate", "0/0"),
                audio_codec=audio.get("codec_name"),
                audio_channels=audio.get("channels"),
                audio_sample_rate=audio.get("sample_rate"),
                color_transfer=video.get("color_transfer"),
                color_space=video.get("color_space"),
                color_primaries=video.get("color_primaries"),
                color_range=video.get("color_range"),
                rotation=rotation % 360,
                sample_aspect_ratio=video.get("sample_aspect_ratio", "1:1"),
                video_streams=len(videos),
                audio_streams=len(audios),
                other_streams=len(data["streams"]) - len(videos) - len(audios),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise OveError("invalid_media", "ffprobe did not return valid video metadata.") from exc
        if info.width * info.height > self.settings.max_pixels:
            raise OveError("resource_limit", "Source exceeds OVE_MAX_PIXELS.")
        if info.duration > self.settings.max_duration_seconds:
            raise OveError("resource_limit", "Source exceeds OVE_MAX_DURATION_SECONDS.")
        return info

    def render(
        self,
        source: Path,
        output: Path,
        info: MediaInfo,
        operations: list[Operation],
        export: ExportSpec,
        cancelled: Callable[[], bool],
    ) -> None:
        video: list[str] = []
        audio: list[str] = []
        rate: Fraction | None = None
        width, height = info.width, info.height
        for op in operations:
            match op:
                case Trim():
                    video += [f"trim=start={op.start}:end={op.end}", "setpts=PTS-STARTPTS"]
                    audio += [f"atrim=start={op.start}:end={op.end}", "asetpts=PTS-STARTPTS"]
                case Resize():
                    video.append(resize_filter(op.width, op.height, op.fit))
                    width, height = op.width, op.height
                case Crop():
                    video.append(f"crop={op.width}:{op.height}:{op.x}:{op.y}")
                    width, height = op.width, op.height
                case Rotate():
                    video.append(
                        {90: "transpose=1", 180: "hflip,vflip", 270: "transpose=2"}[op.degrees]
                    )
                    if op.degrees in {90, 270}:
                        width, height = height, width
                case Flip():
                    video.append("hflip" if op.axis == "horizontal" else "vflip")
                case Speed():
                    # setpts alone collides DTS in the muxer; resample to source rate x factor.
                    video.append(f"setpts=(PTS-STARTPTS)/{op.factor}")
                    rate = (rate or frame_rate_ratio(info.frame_rate)) * Fraction(
                        op.factor
                    ).limit_denominator(1000)
                    factor = op.factor
                    while factor < 0.5:
                        audio.append("atempo=0.5")
                        factor /= 0.5
                    while factor > 2:
                        audio.append("atempo=2")
                        factor /= 2
                    audio.append(f"atempo={factor}")
                case Lighting():
                    video.append(
                        f"eq=brightness={op.brightness}:contrast={op.contrast}:gamma={op.gamma}"
                    )
                case Color():
                    video.append(f"eq=saturation={op.saturation}")
                case Denoise():
                    video.append(f"hqdn3d={op.strength}")
                case Sharpen():
                    video.append(f"unsharp=5:5:{op.amount}:5:5:0")
        if export.width is not None and export.height is not None:
            width, height = export.width, export.height
            video.append(resize_filter(width, height, export.fit))
        if rate is not None:
            video.append(f"fps={rate.numerator}/{rate.denominator}")
        with tempfile.TemporaryDirectory(prefix="ove-ass-") as directory:
            if any(isinstance(op, (Title, Captions)) for op in operations):
                subtitles = Path(directory) / "overlay.ass"
                subtitles.write_text(compose(operations, width, height))
                escaped = (
                    str(subtitles).replace("\\", "\\\\").replace(":", "\\:").replace("'", "'\\''")
                )
                video.append(f"subtitles=filename='{escaped}'")
            arguments = [
                self.settings.ffmpeg_path,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-xerror",
                "-y",
                "-protocol_whitelist",
                "file",
                "-threads",
                str(self.settings.ffmpeg_threads),
                "-format_whitelist",
                "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv",
                "-noautorotate",
                "-i",
                str(source),
                "-map",
                "0:v:0",
            ]
            has_audio = bool(info.audio_codec) and export.audio != "drop"
            if has_audio:
                arguments += ["-map", "0:a:0"]
            if video:
                arguments += ["-vf", ",".join(video)]
            arguments += [
                "-c:v",
                "libx264",
                "-crf",
                str(export.crf),
                "-preset",
                export.encoder_preset,
                "-pix_fmt",
                "yuv420p",
                "-fps_mode",
                "passthrough",
                "-threads",
                str(self.settings.ffmpeg_threads),
            ]
            for field, option in [
                (info.color_transfer, "-color_trc"),
                (info.color_primaries, "-color_primaries"),
                (info.color_space, "-colorspace"),
                (info.color_range, "-color_range"),
            ]:
                if field and field not in {"unknown", "unspecified"}:
                    arguments += [option, field]
            if has_audio:
                if audio:
                    arguments += ["-af", ",".join(audio)]
                if audio or export.audio == "aac":
                    arguments += ["-c:a", "aac", "-b:a", f"{export.audio_bitrate_kbps}k"]
                else:
                    arguments += ["-c:a", "copy"]
            else:
                arguments += ["-an"]
            if export.container == "mp4":
                arguments += ["-movflags", "+faststart"]
            arguments += ["-f", "mp4" if export.container == "mp4" else "matroska", str(output)]
            run_process(
                arguments,
                self.settings.job_timeout_seconds,
                cancelled,
                output,
                self.settings.max_output_bytes,
            )

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
                "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv",
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
                "mov,matroska,webm,avi,mpegts,mpeg,ogg,flv",
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
