"""Host side reads for the shortcut checker (ADR 0007 Decision 2, SH-16).

Every path component is opened relative to its parent without following symlinks, the final
object must be a regular file with a single link, and a read stops one byte past the limit, so
a planted symlink or hard link, a FIFO or an oversized file is refused rather than followed,
waited on or truncated.
"""

import errno
import os
import stat
from pathlib import Path

from rootflake.gate.validator import path_syntax

MAX_FILES = 20
MAX_FILE_BYTES = 1024 * 1024


class CheckerError(Exception):
    pass


def _open_dir(name: str, parent: int) -> int | None:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise CheckerError(f"{name}: {errno.errorcode.get(error.errno, error.errno)}") from None


def read_side(root: Path, path: str) -> str | None:
    """The file's text, or None when it does not exist on this side."""
    if path_syntax(path) is not None:
        raise CheckerError(f"refused path {path!r}")
    parts = path.split("/")
    directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            child = _open_dir(part, directory)
            if child is None:
                return None
            os.close(directory)
            directory = child
        try:
            handle = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise CheckerError(f"{path}: {errno.errorcode.get(error.errno, error.errno)}") from None
        try:
            info = os.fstat(handle)
            if not stat.S_ISREG(info.st_mode):
                raise CheckerError(f"{path}: not a regular file")
            if info.st_nlink != 1:
                raise CheckerError(f"{path}: hard linked")
            with os.fdopen(handle, "rb", closefd=False) as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
        finally:
            os.close(handle)
    finally:
        os.close(directory)
    if len(data) > MAX_FILE_BYTES:
        raise CheckerError(f"{path}: over {MAX_FILE_BYTES} bytes")
    if b"\0" in data:
        raise CheckerError(f"{path}: NUL byte")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise CheckerError(f"{path}: not UTF-8") from None


def extension_candidates(module: str, fqn: str) -> list[str]:
    """Where a top level type may be declared in its referencing file's module, test code
    first (ADR 0007 Decision 2)."""
    prefix = f"{module}/" if module else ""
    rel = fqn.replace(".", "/") + ".java"
    return [f"{prefix}src/test/java/{rel}", f"{prefix}src/main/java/{rel}"]
