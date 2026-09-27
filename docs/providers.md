# Adapter contracts and external dependencies

The composition root in `src/ove/application/bootstrap.py` chooses concrete adapters. The `ports` package defines structural Python protocols. Domain models contain no vendor SDK types.

- `MediaEngine`: capabilities, probe, render, full decode. Return errors on unsupported inputs; never silently change semantics. FFmpeg is implemented.
- `BlobStore`: staged import, immutable publish and local materialization. A future remote store must materialize securely before engine use. Local storage is implemented.
- `Repository`: immutable objects, project revision checks, durable jobs and transitions. SQLite is implemented; multi-worker leasing requires a different implementation and worker coordination strategy.
- `TranscriptionProvider`: capability discovery and normalized transcript output. The optional faster-whisper adapter loads local model files only.
- `DesignProvider`: capabilities and an execution boundary. The bundled Canva implementation always raises `provider_unavailable`. Before implementing calls, replace its broad execution signature with operation-specific validated provider models and policy checks.

## Canva requirements

Credentials alone cannot activate Canva support. An implementation needs a registered application, the documented OAuth authorization-code/PKCE flow, appropriate scopes, secure per-user token storage, actual design/account capability checks, tested asynchronous operations and reconciliation of ambiguous writes.

The verified upstream surfaces and limitations are documented in [architecture section 13](../ARCHITECTURE.md#13-canva-integration-abstraction-and-verified-boundaries). Use Canva's official [REST API documentation](https://www.canva.dev/docs/apps/rest-apis/) and [authentication documentation](https://www.canva.dev/docs/apps/rest-apis/authentication/). Do not assume editor APIs or Canva MCP operations are exposed by REST. Uploading an asset is not proof that it was inserted into a design.

## Local ASR

Install the transcription extra, download a trusted model separately, and record the model version/checksum/license in your deployment inventory. Runtime has no auto-download path. Tests validate the unavailable path without model files; large-model quality and GPU compatibility need separate qualification.

## Future providers

Cloud translation/ASR/enhancement require explicit data-disclosure policy, credentials in a secret store, quotas, timeouts, documented request/response contracts and tests against a real account. No cloud credentials or fake API endpoints are shipped. `OVE_OFFLINE=false` does not enable network calls: none of those adapters exists yet.
