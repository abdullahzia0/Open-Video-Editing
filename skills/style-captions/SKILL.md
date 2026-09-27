---
name: style-captions
description: Style supplied captions with the supported Open Video Editing text templates.
---

Keep wording, cue timing, video geometry and audio unchanged unless requested. Burned-in caption operations accept `modern` or `lower-third` style. These are bounded ASS templates, not arbitrary CSS. Font fallback and wrapping need visual review.

Animated word highlighting is not implemented. Plain SRT/VTT does not carry the template's styling. Braces/backslashes in burned-in text are rejected as ASS control syntax; explain the error or offer a plain sidecar without silently rewriting the words. Inspect the completed output before describing its appearance.
