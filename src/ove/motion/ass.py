"""Safe ASS composition for titles, captions and motion-graphic text overlays.

Only allowlisted templates, positions and animations are emitted. Text is
rejected when it contains ASS control characters, so media-derived strings can
never inject drawing commands into the subtitle renderer.
"""

from collections.abc import Iterable

from ove.domain.errors import OveError
from ove.domain.models import (
    Captions,
    Operation,
    TextOverlay,
    Title,
)

FONT = "DejaVu Sans"

#: Template -> (font scale, bold, boxed, outline, shadow, alignment)
TEMPLATES: dict[str, tuple[float, bool, bool, int, int, int]] = {
    "title": (0.075, True, False, 2, 1, 5),
    "lower_third": (0.048, True, True, 0, 0, 1),
    "callout": (0.042, False, True, 0, 0, 5),
    "social_handle": (0.034, True, True, 0, 0, 3),
    "cta": (0.052, True, True, 0, 0, 2),
    "hook": (0.068, True, True, 0, 0, 8),
    "caption": (0.045, True, False, 2, 1, 2),
}

#: Position -> (fraction of width, fraction of height, ASS alignment)
POSITIONS: dict[str, tuple[float, float, int]] = {
    "center": (0.5, 0.5, 5),
    "top": (0.5, 0.09, 8),
    "bottom": (0.5, 0.88, 2),
    "top_left": (0.07, 0.09, 7),
    "top_right": (0.93, 0.09, 9),
    "bottom_left": (0.07, 0.88, 1),
    "bottom_right": (0.93, 0.88, 3),
}

#: Template -> placement used when the caller does not choose one.
TEMPLATE_POSITION: dict[str, str] = {
    "title": "center",
    "lower_third": "bottom_left",
    "callout": "center",
    "social_handle": "bottom_right",
    "cta": "bottom",
    "hook": "top",
    "caption": "bottom",
}

#: Caption style name -> (bold, boxed, outline, shadow, accent as the box colour)
CAPTION_STYLES: dict[str, tuple[bool, bool, int, int, bool]] = {
    "modern": (True, False, 2, 1, False),
    "lower-third": (True, True, 3, 0, False),
    "bold": (True, False, 3, 2, False),
    "minimal": (False, False, 1, 0, False),
    "highlight": (True, True, 2, 0, True),
}

TYPEWRITER_WORD_LIMIT = 40


def ass_time(value: float) -> str:
    centiseconds = round(value * 100)
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    seconds, fraction = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"


def safe_text(text: str) -> str:
    if any(character in text for character in ("{", "}", "\\")):
        raise OveError(
            "unsupported_text",
            "ASS overlays cannot contain braces or backslashes.",
            "Use plain subtitle sidecars or remove ASS control characters.",
        )
    return text.replace("\n", r"\N")


def ass_color(value: str) -> str:
    """Convert ``0xRRGGBB`` to the ASS ``&HAABBGGRR`` form."""
    if not value.startswith("0x") or len(value) != 8:
        raise OveError("invalid_request", "Colours must use the 0xRRGGBB form.")
    red, green, blue = value[2:4], value[4:6], value[6:8]
    return f"&H00{blue}{green}{red}".upper()


def _style_line(
    name: str,
    font_size: int,
    *,
    bold: bool,
    boxed: bool,
    outline: int,
    shadow: int,
    alignment: int,
    margin_v: int,
    back: str = "&H80000000",
) -> str:
    return (
        f"Style: {name},{FONT},{font_size},&H00FFFFFF,&H000000FF,&H00202020,"
        f"{back},{'-1' if bold else '0'},0,0,0,100,100,0,0,"
        f"{3 if boxed else 1},{outline},{shadow},{alignment},40,40,{margin_v},1\n"
    )


def _animation_tag(animation: str, anchor: tuple[int, int], width: int) -> str:
    x, y = anchor
    if animation == "fade":
        return r"{\fad(200,200)}"
    if animation == "slide_up":
        return rf"{{\move({x},{y + int(width * 0.03)},{x},{y},0,320)}}"
    if animation == "slide_down":
        return rf"{{\move({x},{y - int(width * 0.03)},{x},{y},0,320)}}"
    if animation == "slide_left":
        return rf"{{\move({-width},{y},{x},{y},0,380)}}"
    if animation == "pop":
        return r"{\fscx70\fscy70\t(0,180,\fscx106\fscy106)\t(180,340,\fscx100\fscy100)}"
    return ""


def _overlay_style(overlay: TextOverlay, width: int, height: int) -> tuple[str, str, int, int, str]:
    """Return ``(style_line, style_name, font_size, alignment, position)``."""
    scale, bold, boxed, outline, shadow, _ = TEMPLATES[overlay.template]
    placement = overlay.position or TEMPLATE_POSITION[overlay.template]
    _, _, alignment = POSITIONS[placement]
    font_size = max(16, round(height * scale))
    margin_v = max(20, round(height * 0.08))
    name = f"motion-{overlay.template}"
    style = _style_line(
        name,
        font_size,
        bold=bold,
        boxed=boxed,
        outline=outline,
        shadow=shadow,
        alignment=alignment,
        margin_v=margin_v,
        back=ass_color(overlay.accent_color) if overlay.template == "lower_third" else "&H80000000",
    )
    return style, name, font_size, alignment, placement


def _typewriter_events(overlay: TextOverlay, style_name: str, override: str) -> list[str]:
    words = overlay.text.split()
    if not words or len(words) > TYPEWRITER_WORD_LIMIT:
        return [
            f"Dialogue: 1,{ass_time(overlay.start)},{ass_time(overlay.end)},{style_name},"
            f",0,0,0,,{override}{safe_text(overlay.text)}\n"
        ]
    step = (overlay.end - overlay.start) / len(words)
    events = []
    for index in range(1, len(words) + 1):
        begin = overlay.start + (index - 1) * step
        finish = overlay.end if index == len(words) else overlay.start + index * step
        events.append(
            f"Dialogue: 1,{ass_time(begin)},{ass_time(finish)},{style_name},,0,0,0,,"
            f"{override}{safe_text(' '.join(words[:index]))}\n"
        )
    return events


def _text_overlay_events(overlay: TextOverlay, width: int, height: int) -> tuple[str, list[str]]:
    style, name, font_size, alignment, placement = _overlay_style(overlay, width, height)
    fraction_x, fraction_y, _ = POSITIONS[placement]
    anchor = (int(width * fraction_x), int(height * fraction_y))
    override = rf"{{\an{alignment}}}" + _animation_tag(overlay.animation, anchor, width)
    body = safe_text(overlay.text)
    if overlay.detail:
        body = f"{body}\\N{{\\fs{max(12, round(font_size * 0.62))}}}{safe_text(overlay.detail)}"
    if overlay.animation == "typewriter":
        return style, _typewriter_events(overlay, name, override)
    return style, [
        f"Dialogue: 1,{ass_time(overlay.start)},{ass_time(overlay.end)},{name},,0,0,0,,"
        f"{override}{body}\n"
    ]


def _legacy_events(operation: Title | Captions, events: list[str]) -> None:
    if isinstance(operation, Title):
        animation = r"{\fad(150,150)}" if operation.animated else ""
        events.append(
            f"Dialogue: 1,{ass_time(operation.start)},{ass_time(operation.end)},"
            f"{operation.style},,0,0,0,,{animation}{safe_text(operation.text)}\n"
        )
    else:
        for cue in operation.cues:
            events.append(
                f"Dialogue: 0,{ass_time(cue.start)},{ass_time(cue.end)},{operation.style},"
                f",0,0,0,,{safe_text(cue.text)}\n"
            )


def text_operations(operations: Iterable[Operation]) -> list[Operation]:
    """The subset of operations that require an ASS overlay layer."""
    return [
        operation
        for operation in operations
        if isinstance(operation, Title | Captions | TextOverlay)
    ]


def compose(operations: list[Operation], width: int, height: int) -> str:
    """Build a complete ASS document for the text operations in a plan."""
    font_size = max(18, round(height * 0.045))
    margin = round(height * 0.08)
    document = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
    )
    for name, (bold, boxed, outline, shadow, accent) in CAPTION_STYLES.items():
        document += _style_line(
            name,
            font_size,
            bold=bold,
            boxed=boxed,
            outline=outline,
            shadow=shadow,
            alignment=2 if name != "lower-third" else 1,
            margin_v=margin,
            back="&H00203010" if accent else "&H80000000",
        )
    styles_seen = set(CAPTION_STYLES)
    events: list[str] = []
    for operation in operations:
        if isinstance(operation, TextOverlay):
            name = f"motion-{operation.template}"
            style, overlay_events = _text_overlay_events(operation, width, height)
            if name not in styles_seen:
                styles_seen.add(name)
                document += style
            events.extend(overlay_events)
        else:
            _legacy_events(operation, events)
    return (
        document
        + "[Events]\n"
        + "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "".join(events)
    )
