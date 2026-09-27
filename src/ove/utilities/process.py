"""Bounded subprocess execution with cancellation; never invokes a shell."""

import os
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from ove.domain.errors import OveError


def run_process(
    arguments: list[str],
    timeout: int,
    cancelled: Callable[[], bool] = lambda: False,
    output: Path | None = None,
    max_output_bytes: int = 4_000_000_000,
) -> str:
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise OveError(
                "missing_dependency",
                f"Executable not found: {arguments[0]}",
                "Install FFmpeg/ffprobe or configure their executable paths.",
            ) from exc
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if cancelled():
                    raise OveError("cancelled", "Job was cancelled.")
                if time.monotonic() > deadline:
                    raise OveError("timeout", "Media process exceeded its time limit.")
                if output and output.exists() and output.stat().st_size > max_output_bytes:
                    raise OveError("resource_limit", "Output exceeded the configured byte limit.")
                if os.fstat(stdout.fileno()).st_size > 4_000_000:
                    raise OveError("resource_limit", "Process metadata output exceeded its limit.")
                if os.fstat(stderr.fileno()).st_size > 4_000_000:
                    raise OveError(
                        "resource_limit", "Process diagnostic output exceeded its limit."
                    )
                time.sleep(0.05)
            if cancelled():
                raise OveError("cancelled", "Job was cancelled.")
            if output and output.exists() and output.stat().st_size > max_output_bytes:
                raise OveError("resource_limit", "Output exceeded the configured byte limit.")
            if process.returncode:
                stderr.seek(max(0, os.fstat(stderr.fileno()).st_size - 2000))
                detail = stderr.read(2000).decode(errors="replace")
                raise OveError(
                    "engine_failed",
                    f"Media process failed: {detail}",
                    "Inspect the input and selected codec/filter capabilities.",
                )
            stdout.seek(0)
            return stdout.read(4_000_000).decode(errors="replace")
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait()
