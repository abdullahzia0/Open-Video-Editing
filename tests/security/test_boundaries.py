import os
import sys

import pytest

from ove.domain.errors import OveError
from ove.motion.ass import safe_text
from ove.utilities.process import run_process


def test_denied_path_and_symlink(service, tmp_path):
    with pytest.raises(OveError):
        service.storage.ingest("/etc/passwd")
    link = tmp_path / "link"
    link.symlink_to("/etc/passwd")
    with pytest.raises(OveError):
        service.storage.ingest(str(link))


def test_blob_traversal(service):
    with pytest.raises(OveError):
        service.storage.path("../metadata.sqlite3")


def test_symlink_parent(service, tmp_path):
    link = tmp_path / "outside"
    link.symlink_to("/etc", target_is_directory=True)
    with pytest.raises(OveError):
        service.storage.ingest(str(link / "passwd"))


def test_ingest_limit(service, tmp_path):
    service.settings.max_upload_bytes = 2
    path = tmp_path / "large"
    path.write_bytes(b"123")
    with pytest.raises(OveError):
        service.storage.ingest(str(path))


def test_fifo_does_not_block(service, tmp_path):
    path = tmp_path / "pipe"
    os.mkfifo(path)
    with pytest.raises(OveError):
        service.storage.ingest(str(path))


def test_ass_injection_is_rejected():
    with pytest.raises(OveError):
        safe_text(r"hello{\p1}drawing")


def test_cancel_kills_process_group():
    with pytest.raises(OveError, match="cancelled"):
        run_process([sys.executable, "-c", "import time; time.sleep(20)"], 5, lambda: True)


def test_timeout():
    with pytest.raises(OveError, match="time limit"):
        run_process([sys.executable, "-c", "import time; time.sleep(20)"], 1)
