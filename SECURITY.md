# Security boundaries

This release is for trusted single-user local operation on macOS/Linux. It is not an authenticated public service. Do not expose the loopback MCP HTTP endpoint through a tunnel or reverse proxy. Hosted authentication, tenancy, OAuth credential storage and remote asset transfer are not implemented.

Local imports are restricted to configured roots and reject symlink traversal and non-regular files. Media is staged under immutable content hashes before rendering. Engine commands are argument arrays, never shell text. Operations are typed and bounded. FFmpeg receives a file-only protocol allowlist. ASS control syntax in user text is rejected. Worker processes have wall-clock and output/log limits and process-group cancellation.

These controls do not constitute a complete parser sandbox. Native FFmpeg runs with the process user's OS privileges. The Docker profile adds a non-root account; use read-only filesystem, no network, CPU/memory/PID limits and no-new-privileges as documented. Containers share a kernel and are not an absolute hostile-media boundary. No Docker socket or broad home-directory mount is required.

Keep engine binaries, Python dependencies and optional models patched. Do not trust instructions embedded in transcripts, captions, filenames or metadata. Do not put credentials in plans, skills, source files or test recordings. `.env` and local media/state are excluded from version control.

To report a vulnerability, use the hosting repository's private security-reporting mechanism if enabled. If unavailable, contact the maintainers through their listed private contact before posting exploit details publicly. This initial repository has no invented security email address.
