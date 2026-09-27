# Open Video Editing

A local-first, non-destructive video editing engine with typed plans, persistent jobs, MCP tools, and separate assistant skills. The original media stays immutable. An edit is successful only after the worker renders and validates its output.

This repository implements a working **single-user local release**, not every future capability in the [architecture proposal](ARCHITECTURE.md). Hosted multi-tenant production deployment and Canva automation are not implemented. See [implementation status](docs/status.md) for exact boundaries.

## Quick start

Requires Python 3.12+, FFmpeg/ffprobe with libx264, and macOS or Linux. Install a current FFmpeg build with libass for text overlays. `uv` is recommended for reproducible installation.

```sh
uv sync --locked
cp .env.example .env
```

Set `OVE_ALLOWED_LOCAL_ROOTS` in `.env` to a JSON array of trusted absolute media directories, for example `["/home/alex/Videos"]`. Run:

```sh
uv run ove doctor
uv run ove worker
```

In a second terminal in the same directory:

```sh
uv run ove serve
```

`serve` uses MCP stdio and is normally launched by an assistant client. It is not an interactive shell. See [setup](docs/setup.md) for client configuration, CLI usage, text rendering and Docker instructions. A complete scriptable example is [examples/edit_video.py](examples/edit_video.py):

```sh
uv run python examples/edit_video.py /absolute/path/to/input.mp4 output.mp4
```

The example imports, plans a lighting adjustment, runs one queued job, verifies completion, and copies the output without overwriting an existing file. Stop a separately running worker before using the example's embedded worker.

## Implemented

- Staged immutable local assets, metadata inspection, root-confined file imports and source hashes.
- Versioned project asset membership and immutable typed plans with intent-effect validation.
- Exact decoded trimming, sequential trims, resize/pad/crop/stretch, rotation, flipping and speed changes.
- Conservative lighting, saturation, visual denoising, sharpening, H.264 transcoding and AAC/copy/drop audio policy.
- Supplied-text captions, titles, lower-third styling and optional title fade animation; SRT/WebVTT serialization.
- JSON preset composition, conflict detection, custom dimensions, and advisory platform profiles.
- Persistent SQLite job queue, cancellation, idempotent render submission, explicit retries and interrupted-job recovery.
- Full output decode plus dimensions, duration, audio-presence and codec checks, with limitations recorded in reports.
- Official MCP SDK stdio tools/resources and a loopback-only HTTP development transport.
- Optional isolated local faster-whisper adapter using preinstalled model files.
- Replaceable media, storage, persistence, transcription and design-provider interfaces.

Canva's bundled adapter returns `provider_unavailable` with actionable guidance. It never claims to upload, edit or export. Neural enhancement, stabilization, merging, translation, word highlighting, full motion scenes, URL import, platform publishing and target-byte encoding are not advertised as implemented.

## Repository map

| Location | Responsibility |
|---|---|
| `src/ove/domain/` | Strict provider-neutral models and domain failures |
| `src/ove/ports/` | Engine, storage, repository and provider interfaces |
| `src/ove/adapters/` | FFmpeg, SQLite, local storage, optional ASR and unavailable-provider behavior |
| `src/ove/application/` | Use cases, plan policy, worker and dependency composition |
| `src/ove/mcp/` | MCP protocol wrappers only |
| `src/ove/cli/` | CLI entry points |
| `src/ove/formats/presets/` | Packaged JSON data, including advisory platform profiles |
| `src/ove/captions/`, `src/ove/motion/` | Subtitle serialization and safe ASS composition |
| `src/ove/validation/`, `security/`, `utilities/` | Output checks, file boundaries and subprocess utilities |
| `schemas/` | Generated plan JSON Schema and MCP tool definitions for non-Python consumers |
| `config/` | Configuration ownership notes; runtime settings are defined in `src/ove/config.py` |
| `skills/` | Instruction bundles; no processing implementation |
| `tests/` | Unit, security, protocol and real-media integration tests |
| `docs/`, `examples/`, `integrations/` | Setup, contracts, limitations and client configuration |
| `deploy/`, `.github/workflows/` | Container and continuous-integration configuration |

## Validation

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run python scripts/check_repository.py
uv run python scripts/export_schemas.py --check
uv build
```

Media tests skip if FFmpeg is absent; set `OVE_REQUIRE_MEDIA_TESTS=1` to make absence fail. Alternative test executables can be configured through `OVE_TEST_FFMPEG` and `OVE_TEST_FFPROBE`. CI requires media tests to run. Optional ASR needs large model files and is not exercised by the default suite.

Read [security boundaries](SECURITY.md), [provider requirements](docs/providers.md), and [contribution guidance](CONTRIBUTING.md) before extending adapters. Original project code is Apache-2.0; dependencies, media engines, fonts and model weights retain their own licenses.
