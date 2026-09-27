# Contributing

Use Python 3.12+, `uv sync --locked`, and an FFmpeg build with libx264 and libass. Run the checks listed in README before submitting changes. Set `OVE_REQUIRE_MEDIA_TESTS=1` for media-related contributions so missing binaries fail rather than silently skipping coverage.

Preserve the architecture boundaries: domain/ports contain no MCP or provider SDK imports; tools dispatch to application services; media processing belongs in an adapter; presets are data; skills are instructions. Add schema validation and actionable errors before advertising a capability. No stub may return success for an operation it did not perform.

For an adapter contribution, include normalized request/result contracts, runtime capability discovery, error/timeout/cancellation behavior, license notes, offline behavior and tests. Live external tests must be opt-in and use disposable authorized assets. Do not record tokens, private media or signed URLs in fixtures.

Update `docs/status.md` and generated schemas when public behavior changes. Preserve immutable plan semantics or introduce a new schema version. Add tests for behavior and failure boundaries rather than exact implementation details. Use synthetic or redistributable media and identify its provenance.

Skills should remain short, name actual tools, avoid expanding user intent, and report only verified outcomes. Keep task-specific instructions separate from executable engine code.

Original contributions are licensed under Apache-2.0 as specified in LICENSE. Third-party code, fonts, models and binaries need their own license attribution and distribution review.
