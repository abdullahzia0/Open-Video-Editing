---
name: open-video-editing
description: Route natural-language video edits through Open Video Editing MCP tools and verify resulting assets.
---

Read `system_capabilities` first. Use only reported operations. Import an authorized local source, create or retrieve its project, then construct a typed plan with the user's actual request and only the permitted effects. `allowed_effects` is a scope declaration, not a way to grant new permission.

“Only improve lighting” permits `lighting`, not geometry, timing, captions, denoise, sharpening or external disclosure. If “trim the first five seconds” is ambiguous, resolve whether to remove or retain that interval. “Instagram-ready” needs a specific surface and framing choice when content could be cropped.

Call `plans_create`, inspect warnings and expected changes, then `render_submit` with the exact ID/hash. An existing authorization for the requested edit is sufficient; ask only for consequential unresolved choices. A separate local worker must be running. Poll `jobs_get`; return `artifacts_get` only after success. Report failures and incomplete steps honestly. Do not describe a local file path as a hosted download link.
