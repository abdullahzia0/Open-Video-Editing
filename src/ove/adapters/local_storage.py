"""Immutable content-addressed local storage with bounded ingestion."""

import hashlib
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path

from ove.config import Settings
from ove.domain.errors import OveError
from ove.security.files import open_allowed_file


class LocalBlobStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir / "blobs"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.scratch = settings.data_dir / "scratch"
        self.scratch.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, key: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", key):
            raise OveError("invalid_blob", "Invalid blob identifier.")
        target = self.root / key
        if not target.is_file() or target.is_symlink():
            raise OveError("not_found", "Blob is missing or unsafe.")
        return target

    def ingest(self, source: str) -> tuple[str, str, int]:
        descriptor = open_allowed_file(source, self.settings.allowed_local_roots)
        temporary: Path | None = None
        try:
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise OveError("invalid_source", "Source must be a regular file.")
                if info.st_size > self.settings.max_upload_bytes:
                    raise OveError("resource_limit", "Source exceeds OVE_MAX_UPLOAD_BYTES.")
                with tempfile.NamedTemporaryFile(dir=self.scratch, delete=False) as target:
                    temporary = Path(target.name)
                    total = 0
                    while chunk := stream.read(1024 * 1024):
                        total += len(chunk)
                        if total > self.settings.max_upload_bytes:
                            raise OveError("resource_limit", "Source grew beyond the upload limit.")
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            return self.publish(temporary)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    def publish(self, temporary: Path) -> tuple[str, str, int]:
        checksum = hashlib.sha256()
        size = 0
        with temporary.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                checksum.update(chunk)
                size += len(chunk)
        if size == 0:
            raise OveError("empty_output", "An empty file cannot be published.")
        key = checksum.hexdigest()
        destination = self.root / key
        if not destination.exists():
            # Copy to the same filesystem before atomic publication.
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as staged:
                staged_path = Path(staged.name)
                try:
                    with temporary.open("rb") as source:
                        shutil.copyfileobj(source, staged)
                    staged.flush()
                    os.fsync(staged.fileno())
                    os.chmod(staged_path, 0o400)
                    os.replace(staged_path, destination)
                finally:
                    staged_path.unlink(missing_ok=True)
        return key, key, size
