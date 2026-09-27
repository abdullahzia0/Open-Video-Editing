# Setup and operation

## Native setup

Use Python 3.12 or newer on macOS/Linux. Install FFmpeg and ffprobe from a trusted distribution, then `uv sync --locked`. The default install includes development tools; `uv sync --locked --no-dev` is appropriate for a runtime installation. A wheel can also be installed using `pip install .` in a suitable virtual environment; it does not bundle FFmpeg.

Copy `.env.example` to `.env` and set absolute `OVE_ALLOWED_LOCAL_ROOTS`. Empty roots intentionally deny imports. Data defaults to `.ove` relative to the process working directory. Use an absolute `OVE_DATA_DIR` when a client launches from a different directory. Both the MCP process and worker must use the same configuration and data directory. The process running the server has the local OS user's privileges.

`ove doctor` checks installed filters and encoders. Text overlays need the `subtitles` filter, libass and a usable font. The templates request DejaVu Sans; other installed fonts may be substituted by libass. Font substitution and visual clipping require preview review.

## CLI workflow

1. `ove import /absolute/path/video.mp4` returns an asset ID and probe metadata.
2. `ove project "My edit" asset_ID` returns a project ID and revision.
3. Fill those IDs into a plan matching [the schema](../schemas/plan.schema.json). The [Python example](../examples/edit_video.py) builds the document without manual ID replacement.
4. `ove plan plan.json` validates scope, parameters and installed engine capabilities. Save the returned ID and hash.
5. `ove render plan_ID HASH --key unique-request-key` queues a durable job.
6. Run `ove worker` continuously, or `ove worker --once` to process at most one job.
7. `ove job job_ID` returns state and, after success, an artifact ID.
8. `ove artifact artifact_ID --output edited.mp4` copies the validated artifact to a new file.

`ove cancel job_ID` is idempotent. Queued jobs cancel immediately; running FFmpeg processes observe cancellation through the worker. On worker restart, interrupted running jobs become failed with `worker_interrupted`. Use MCP `jobs_retry` with a new key to retry; rendering always rechecks the immutable plan and current engine.

Trim bounds are seconds in the current timeline and half-open `[start, end)`. A trim retains that interval. To remove the opening five seconds, retain `[5, source_duration)`. Text operations must follow timing/geometry edits; text times refer to the resulting timeline. This release operates on one source asset per plan, with ordered edits rather than a multi-track timeline.

## MCP clients

The [generic template](../integrations/generic-mcp/mcp.json) and [Claude template](../integrations/claude/mcp.json) use `uv --directory /ABSOLUTE/PATH/TO/REPOSITORY run --locked ove serve`. Replace the path. Start the worker separately. JSON templates are examples, not automatic installation into your client.

Claude clients with local stdio support can launch this process. For remote-only clients, including hosted ChatGPT connections, additional authenticated public hosting and file transfer are required. This release deliberately does not expose an unauthenticated public service. Read [ChatGPT integration notes](../integrations/chatgpt/README.md).

For local protocol development only, `ove serve --transport streamable-http` binds to `127.0.0.1:8765` with endpoint `/mcp`. Do not tunnel or publicly proxy it: user authentication and tenancy are not implemented. Install skills separately using the target host's skill installation mechanism; MCP discovery does not install instruction files.

## Optional transcription

`uv sync --locked --extra transcription` installs faster-whisper. Obtain a trusted CTranslate2 model and its license separately, then configure:

```dotenv
OVE_TRANSCRIPTION_PROVIDER=faster-whisper
OVE_TRANSCRIPTION_MODEL_DIR=/absolute/path/to/local/model
OVE_TRANSCRIPTION_DEVICE=cpu
OVE_TRANSCRIPTION_COMPUTE_TYPE=int8
```

No model is downloaded automatically. Capabilities check package/model presence; actual model initialization may still fail due to incomplete files or hardware incompatibility. ASR is executed in a cancellable subprocess. Word timings are model estimates, and speech accuracy requires review. No API credentials are required for local transcription.

## Docker

Build from the repository root:

```sh
docker build -f deploy/docker/Dockerfile -t open-video-editing:local .
```

The image includes CPU FFmpeg and fonts, runs as UID 10001, and has `/data` as the persistent data directory. Initialize a host directory owned by that UID, then launch a worker with read-only root filesystem, constrained resources and no network:

```sh
docker run --rm --read-only --network none --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 128 --memory 2g --cpus 2 \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  -v /absolute/ove-data:/data \
  -v /absolute/media:/media:ro \
  open-video-editing:local worker
```

Run another container with the same mounts and `-i` for stdio MCP, replacing `worker` with `serve`. Do not use `-t`: a terminal can corrupt protocol framing. See [Compose configuration](../deploy/compose.yaml) for the persistent worker profile. No HTTP port is published. GPU/ASR images and hosted deployment are future work.

## Lifecycle

Preserve `.ove/metadata.sqlite3` together with `.ove/blobs`. Stop both processes before a simple file-copy backup, or use a consistent SQLite backup procedure. Never edit database rows by hand. Orphan blob/scratch garbage collection, retention automation and schema upgrades are not implemented; review storage manually while stopped and keep a backup. Content hashes deduplicate within this single-user store only.

## Dependency compatibility

The lockfile uses the maintained MCP SDK 1.x API (`mcp<2`); an SDK 2 migration requires updating protocol tests. Intel macOS currently constrains the transitive `cryptography` dependency to the 46.x wheel line because newer wheels require a newer macOS runtime and the source build needs additional OpenSSL tooling. Review this constraint during dependency updates; other platforms use the unconstrained transitive version selected in the lockfile. No cryptographic routines are implemented by this project.
