"""Deterministic, template-driven copy suggestions.

This module deliberately does **not** call a language model. A local, offline
editing server cannot honestly claim generated copy, so every suggestion is
composed from an explicit template plus the caller's own subject and details.
The MCP layer labels these results ``source: "template"`` and
``model_generated: false`` so an assistant never presents them as AI authorship.
"""

from typing import Literal

from ove.domain.errors import OveError

Kind = Literal["title", "hook", "cta"]

#: Templates per kind. ``{subject}`` is required; ``{detail}`` and ``{audience}``
#: are optional and their clauses are dropped when the caller supplies nothing.
TEMPLATES: dict[Kind, tuple[str, ...]] = {
    "title": (
        "{subject}",
        "{subject}: {detail}",
        "How {subject} Works",
        "The Complete Guide to {subject}",
        "{subject} Explained in 60 Seconds",
        "{subject} for {audience}",
        "Why {subject} Matters",
    ),
    "hook": (
        "Most people get {subject} wrong. Here is what actually works.",
        "If {subject} has ever confused you, watch this.",
        "Three things nobody tells you about {subject}.",
        "I tested {subject} so you do not have to.",
        "Stop scrolling if you care about {subject}.",
        "This changes how you think about {subject}.",
        "{detail} — and it starts with {subject}.",
    ),
    "cta": (
        "Follow for more on {subject}.",
        "Save this before you need it.",
        "Comment your take on {subject} below.",
        "Subscribe for the full {subject} series.",
        "Share this with someone who needs {subject}.",
        "Link in the description for the full breakdown.",
        "Try it yourself and tell me how it went.",
    ),
}

MAX_SUGGESTIONS = 7


def suggestions(
    kind: Kind,
    subject: str,
    detail: str | None = None,
    audience: str | None = None,
    limit: int = 5,
) -> list[str]:
    """Compose up to ``limit`` distinct suggestions from the template table."""
    subject = " ".join(subject.split())
    if not subject:
        raise OveError("invalid_input", "A non-empty subject is required.")
    if len(subject) > 120:
        raise OveError("invalid_input", "Keep the subject under 120 characters.")
    if limit < 1 or limit > MAX_SUGGESTIONS:
        raise OveError("invalid_input", f"limit must be between 1 and {MAX_SUGGESTIONS}.")

    rendered: list[str] = []
    for template in TEMPLATES[kind]:
        needs_detail = "{detail}" in template
        needs_audience = "{audience}" in template
        if needs_detail and not detail:
            continue
        if needs_audience and not audience:
            continue
        candidate = template.format(
            subject=subject,
            detail=(detail or "").strip(),
            audience=(audience or "").strip(),
        )
        candidate = " ".join(candidate.split())
        if candidate and candidate not in rendered:
            rendered.append(candidate)
        if len(rendered) >= limit:
            break
    if not rendered:
        raise OveError(
            "invalid_input",
            "No template matched the supplied subject and details.",
            "Supply a subject, or add a detail/audience for the templates that need one.",
        )
    return rendered
