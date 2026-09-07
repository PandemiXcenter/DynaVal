import os
import subprocess
from pathlib import Path

import pytest

from dynaval.storage.locking import SessionLock, SessionLockedError


def test_lock_is_exclusive_and_reusable(tmp_path: Path) -> None:
    first = SessionLock(tmp_path / "writer.lock")
    second = SessionLock(tmp_path / "writer.lock")
    first.acquire()
    try:
        with pytest.raises(SessionLockedError):
            second.acquire()
        with pytest.raises(SessionLockedError):
            first.acquire()
    finally:
        first.release()
    first.release()
    with second:
        assert second.path.stat().st_size >= 1


def test_cross_process_writer_lock(tmp_path: Path) -> None:
    path = tmp_path / "writer.lock"
    code = """
import sys
from pathlib import Path
from dynaval.storage.locking import SessionLock, SessionLockedError
try:
    with SessionLock(Path(sys.argv[1])):
        pass
except SessionLockedError:
    sys.exit(2)
"""

    def attempt() -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["uv", "run", "--no-sync", "python", "-c", code, str(path)],
            capture_output=True,
            timeout=30,
            env={
                **os.environ,
                "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(tmp_path / "uv-cache")),
            },
        )

    with SessionLock(path):
        assert attempt().returncode == 2
    result = attempt()
    assert result.returncode == 0, result.stderr.decode()
