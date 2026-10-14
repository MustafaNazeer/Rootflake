"""Reads IDoFT's `pr-data.csv` at a pinned commit, verified by its SHA256.

The file is never committed (IDoFT declares no license); it is fetched once into the local
cache and checked before every use, so every count traces to exactly these bytes.
"""

import csv
import hashlib
import io
import os
import re
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

PINNED_COMMIT = "4903dc9233eead4e4191c66f9ed0b14eea091052"
PINNED_SHA256 = "4d9a8b9a0cbb597792efe328f4bf29300de585681b77d97fdaa98c90d96f93fb"
HEADER = (
    "Project URL",
    "SHA Detected",
    "Module Path",
    "Fully-Qualified Test Name (packageName.ClassName.methodName)",
    "Category",
    "Status",
    "PR Link",
    "Notes",
)
RAW_URL = "https://raw.githubusercontent.com/TestingResearchIllinois/idoft/{commit}/pr-data.csv"
FETCH_TIMEOUT_S = 120
MAX_BYTES = 64 * 1024 * 1024

_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_CLASS_PART_RE = re.compile(r"[A-Za-z0-9_$.]*")
_JAVA_NAME_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*")

Opener = Callable[[str, float], bytes]


@dataclass(frozen=True)
class Row:
    idoft_row: int
    project_url: str
    sha_detected: str
    module_path: str
    test_name: str
    category: str
    status: str
    pr_url: str


def parse(data: bytes, expected_sha256: str = PINNED_SHA256) -> list[Row]:
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"pr-data.csv SHA256 is {actual}, expected {expected_sha256}")
    reader = csv.reader(io.StringIO(data.decode("utf-8"), newline=""))
    if tuple(next(reader, ())) != HEADER:
        raise ValueError("pr-data.csv header differs from the pinned header")
    rows = []
    for number, fields in enumerate(reader, start=1):
        if len(fields) != len(HEADER):
            raise ValueError(f"row {number} has {len(fields)} columns, expected {len(HEADER)}")
        project, sha, module, test, category, status, pr, _notes = fields
        rows.append(Row(number, project, sha, module, test, category, status, pr))
    return rows


def derive_test_class(test_name: str) -> str:
    """Cut at the first character outside a Java name, then drop the method segment."""
    test_class = _CLASS_PART_RE.match(test_name).group().rpartition(".")[0]
    if not _JAVA_NAME_RE.fullmatch(test_class):
        raise ValueError(f"no class in test name {test_name!r}")
    return test_class


def cache_path(root: Path, commit: str) -> Path:
    return root / "idoft" / commit / "pr-data.csv"


def _download(url: str, timeout: float) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError(f"{url} is larger than {MAX_BYTES} bytes")
    return data


def fetch(
    root: Path,
    commit: str = PINNED_COMMIT,
    expected_sha256: str = PINNED_SHA256,
    opener: Opener = _download,
) -> Path:
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError(f"commit must be a full lowercase SHA: {commit!r}")
    data = opener(RAW_URL.format(commit=commit), FETCH_TIMEOUT_S)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected_sha256:
        raise ValueError(f"downloaded pr-data.csv SHA256 is {actual}, expected {expected_sha256}")
    path = cache_path(root, commit)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    partial.write_bytes(data)
    os.replace(partial, path)
    return path
