"""Reads a Surefire report from a workspace the sandbox user could have tampered with.

Every path component is opened with O_NOFOLLOW relative to its parent, so a planted symlink
cannot point the read outside the workspace, and only a regular file is read, so a FIFO or
device cannot hang the host or stream forever.
"""

import os
import stat
from pathlib import Path

from rootflake.sandbox.results import MAX_REPORT_BYTES


def read_report(workspace: Path, relative: Path, limit: int = MAX_REPORT_BYTES) -> bytes | None:
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"report path must stay inside the workspace: {relative}")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fds = []
    try:
        try:
            fds.append(os.open(workspace, flags | os.O_DIRECTORY))
            for part in relative.parts[:-1]:
                fds.append(os.open(part, flags | os.O_DIRECTORY, dir_fd=fds[-1]))
            fd = os.open(relative.parts[-1], flags | os.O_NONBLOCK, dir_fd=fds[-1])
        except OSError:
            return None
        fds.append(fd)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks = []
        remaining = limit + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        for fd in reversed(fds):
            os.close(fd)
