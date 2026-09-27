"""Root-confined POSIX file opening with no symlink traversal."""

import os
from pathlib import Path

from ove.domain.errors import OveError


def open_allowed_file(value: str, roots: list[Path]) -> int:
    candidate = Path(os.path.abspath(os.path.expanduser(value)))
    for root in roots:
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        if not relative.parts:
            break
        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in relative.parts[:-1]:
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
            return os.open(
                relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except OSError as exc:
            raise OveError(
                "invalid_source", "Cannot open source without following symlinks."
            ) from exc
        finally:
            os.close(directory)
    raise OveError(
        "path_denied",
        "The source is outside configured local roots.",
        "Set OVE_ALLOWED_LOCAL_ROOTS to a JSON array of trusted absolute directories.",
    )
