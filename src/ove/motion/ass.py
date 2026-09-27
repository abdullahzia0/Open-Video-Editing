"""Safe ASS title/caption composition with bounded fade animation."""

from ove.domain.errors import OveError
from ove.domain.models import Captions, Operation, Title


def ass_time(value: float) -> str:
    centiseconds = round(value * 100)
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"


def safe_text(text: str) -> str:
    if any(c in text for c in ("{", "}", "\\")):
        raise OveError(
            "unsupported_text",
            "ASS overlays cannot contain braces or backslashes.",
            "Use plain subtitle sidecars or remove ASS control characters.",
        )
    return text.replace("\n", r"\N")


def compose(operations: list[Operation], width: int, height: int) -> str:
    font_size = max(18, round(height * 0.045))
    margin = round(height * 0.08)
    text = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: modern,DejaVu Sans,{font_size},&H00FFFFFF,&H000000FF,&H00202020,"
        f"&H80000000,-1,0,0,0,100,100,0,0,1,2,1,2,40,40,{margin},1\n"
        f"Style: lower-third,DejaVu Sans,{font_size},&H00FFFFFF,&H000000FF,&H00202020,"
        f"&H80000000,-1,0,0,0,100,100,0,0,3,3,0,1,40,40,{margin},1\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    for op in operations:
        if isinstance(op, Title):
            animation = r"{\fad(150,150)}" if op.animated else ""
            text += (
                f"Dialogue: 1,{ass_time(op.start)},{ass_time(op.end)},{op.style},,0,0,0,,"
                f"{animation}{safe_text(op.text)}\n"
            )
        elif isinstance(op, Captions):
            for cue in op.cues:
                text += (
                    f"Dialogue: 0,{ass_time(cue.start)},{ass_time(cue.end)},{op.style},,0,0,0,,"
                    f"{safe_text(cue.text)}\n"
                )
    return text
