"""Owner-only file writes."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def write_private(path: Path, text: str) -> None:
    """Atomically write text to path with mode 0600, never following a symlink.

    The temp file has a unique name (mkstemp: 0600, O_EXCL), so two writers
    can't swap half-written files, and it's synced before the rename so a
    power cut leaves the old file or the new one, not an empty one.
    """
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
