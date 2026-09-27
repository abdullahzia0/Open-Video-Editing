# Implementation status and architecture deviations

`ARCHITECTURE.md` remains the full target architecture. This release implements its local foundation and selected editing/caption features. No claims in the proposal override runtime capability discovery.

| Area | Current state |
|---|---|
| Timeline | One source per plan, ordered typed operations; project revisions track asset membership |
| Formats | Data-driven fragment composition, 16 advisory JSON profiles, custom even dimensions, MP4/MKV with H.264 |
| Editing | Trim, resize/pad/crop/stretch, rotate, flip, speed, lighting, saturation, denoise, sharpen |
| Text/motion | Supplied caption cues, SRT/VTT, ASS titles/lower-third style and fade animation |
| ASR | Optional faster-whisper subprocess adapter; local package and model required; not default-suite model tested |
| Canva | DesignProvider interface and truthful unavailable adapter; no network calls or credential handling |
| Storage | Immutable local content-addressed blobs; import restricted to configured local roots |
| Queue | SQLite, single exclusive POSIX worker, idempotent render jobs, cancellation, explicit retry |
| MCP | 20 tools, two resources, official v1 SDK; stdio and loopback HTTP development transport |
| Validation | Full output decode, dimensions/duration/audio presence/H.264 checks; reports disclose missing perceptual checks |
| Deployment | Native macOS/Linux and CPU Docker configuration; no hosted multi-tenant release |

Advanced stabilization, merge/split-file export, freeze frames, motion scene graphs, LUT grading, neural enhancement, translation, diarization, animated word highlighting, standalone full QC reruns, transcript editing/versioning, remote file transfer, user authentication, cloud storage, PostgreSQL, retention jobs and social publishing remain future work. They are not success-returning stubs.

Odd dimensions, HDR, rotation metadata, non-square pixels, multiple video/audio streams and extra streams are rejected when a safe preserving pipeline is unavailable. Missing FFmpeg filters are detected. A request permitting format conversion can accept an 8-bit output from some non-HDR source pixel formats. The service never silently claims to preserve HDR.

Speed changes resample video to a constant frame rate of source rate x factor; variable-frame-rate timestamps are not preserved, and the plan records that limitation as a warning.

Plan policy enforces declared operation effects, not the truth of an assistant's interpretation of unseen conversation. Users/hosts must supply accurate intent. No security authorization is inferred from `allowed_effects`: the local process already runs as the trusted OS user, and there is no remote tenant boundary.

Platform preset names are editing defaults, not verified current platform acceptance limits. Full encode/decode checks cannot prove aesthetics, intelligibility, glyph coverage, color correctness or perceptual A/V synchronization. Preview outputs before publication.
