# Contracts and errors

Generated [plan schema](../schemas/plan.schema.json) and [MCP tool definitions](../schemas/mcp-tools.json) describe the executable interface. Regenerate with `uv run python scripts/export_schemas.py`; CI compares the committed files with current definitions.

MCP calls return a structured envelope with `schema_version`, `request_id`, and either `data` or `error`. Errors also set MCP `isError=true`. Error fields include `code`, `message`, `action`, and `retryable` where applicable. An accepted job returns `queued`, not an artifact.

Typical errors: `scope_violation`, `invalid_range`, `preset_conflict`, `path_denied`, `unsupported_media`, `missing_dependency`, `provider_unavailable`, `plan_mismatch`, `engine_changed`, `idempotency_conflict`, `revision_conflict`, `worker_interrupted`, `timeout`, `cancelled`, `validation_failed`.

Sources and plans are immutable. Plans bind a source checksum and an engine capability/version fingerprint. Reusing a render idempotency key with a different plan fails. Retries use a new key and preserve the original operation semantics. Local asset imports and project creation allocate new records and are not idempotency-key APIs; the stronger target-architecture requirement is deferred for these metadata operations.

Jobs persist after the client disconnects. An exclusive worker lock prevents two local workers from claiming work in the same data directory. A worker restart marks in-progress jobs interrupted, rather than falsely claiming arbitrary FFmpeg resumption. Artifacts cannot be delivered through the service until their producing job succeeds. Cancellation wins a race with completion; orphan files may remain until manual cleanup.

Use a separate import/upload design for remote clients. A local asset path is not a hosted download URL. Do not insert private local paths into externally shareable links.
