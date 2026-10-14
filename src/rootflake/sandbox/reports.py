"""Reads a Surefire report from a workspace the sandbox user could have tampered with.

Every path component is opened with O_NOFOLLOW relative to its parent, so a planted symlink
cannot point the read outside the workspace, and only a regular file is read, so a FIFO or
device cannot hang the host or stream forever.

Reruns share one workspace, so a report left by an earlier run is not evidence about the next
one. The caller stamps the report before a run and passes the stamp back as `stale`; a report
whose stamp has not changed reads as missing.
"""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from rootflake.sandbox.results import MAX_REPORT_BYTES

ReportStamp = tuple[int, int, int, int]


class ReportReader(Protocol):
    def __call__(
        self, workspace: Path, relative: Path, *, stale: ReportStamp | None = None
    ) -> bytes | None: ...


@contextmanager
def _open_regular(workspace: Path, relative: Path) -> Iterator[int | None]:
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"report path must stay inside the workspace: {relative}")
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    fds = []
    try:
        try:
            fds.append(os.open(workspace, flags | os.O_DIRECTORY))
            for part in relative.parts[:-1]:
                fds.append(os.open(part, flags | os.O_DIRECTORY, dir_fd=fds[-1]))
            fds.append(os.open(relative.parts[-1], flags | os.O_NONBLOCK, dir_fd=fds[-1]))
        except OSError:
            yield None
            return
        yield fds[-1] if stat.S_ISREG(os.fstat(fds[-1]).st_mode) else None
    finally:
        for fd in reversed(fds):
            os.close(fd)


def _stamp(fd: int) -> ReportStamp:
    info = os.fstat(fd)
    return (info.st_dev, info.st_ino, info.st_ctime_ns, info.st_size)


def report_stamp(workspace: Path, relative: Path) -> ReportStamp | None:
    with _open_regular(workspace, relative) as fd:
        return None if fd is None else _stamp(fd)


def read_report(
    workspace: Path,
    relative: Path,
    limit: int = MAX_REPORT_BYTES,
    stale: ReportStamp | None = None,
) -> bytes | None:
    with _open_regular(workspace, relative) as fd:
        if fd is None or (stale is not None and _stamp(fd) == stale):
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
