"""Subtitle serialization, independent of transcription and transport."""

from ove.domain.models import CaptionCue


def timestamp(seconds: float, separator: str = ",") -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}{separator}{milliseconds:03}"


def serialize(cues: list[CaptionCue], format_name: str) -> str:
    if format_name not in {"srt", "vtt"}:
        raise ValueError("format_name must be srt or vtt")
    blocks = []
    for index, cue in enumerate(cues, 1):
        # Block separators would create unintended subtitle records.
        text = "\n".join(line for line in cue.text.splitlines() if line.strip())
        separator = "." if format_name == "vtt" else ","
        blocks.append(
            f"{index}\n{timestamp(cue.start, separator)} --> "
            f"{timestamp(cue.end, separator)}\n{text}\n"
        )
    return ("WEBVTT\n\n" if format_name == "vtt" else "") + "\n".join(blocks)
