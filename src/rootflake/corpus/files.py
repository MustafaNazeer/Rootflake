"""Writes one corpus version directory byte for byte reproducibly (ADR 0006, Decisions 2 and 6).

A version is written into a temporary sibling and renamed into place, and an existing
version is never touched, so a frozen split file cannot be overwritten by a rebuild.
"""

import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from rootflake.corpus.funnel import ScreenResult
from rootflake.corpus.walk import Drawn, WalkResult

SPLITS = ("dev", "test")


def dumps_line(obj: object) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _jsonl(objects: list[dict]) -> bytes:
    return "".join(dumps_line(obj) + "\n" for obj in objects).encode("utf-8")


def funnel_rows(screened: ScreenResult, walked: WalkResult) -> list[dict]:
    """One row per Accepted row, so the counts add up: rule 2 to 5 reasons and walk outcomes."""
    overlap = set(screened.reasons) & set(walked.reasons)
    if overlap:
        raise ValueError(f"rows with a screen and a walk reason: {sorted(overlap)}")
    listed = set(screened.reasons) | set(walked.reasons)
    if listed != screened.accepted:
        missing, extra = sorted(screened.accepted - listed), sorted(listed - screened.accepted)
        raise ValueError(f"funnel rows without a reason: {missing}; not Accepted: {extra}")
    rows = [
        {"idoft_row": n, "reason": reason, "stratum": screened.strata.get(n), "detail": None}
        for n, reason in screened.reasons.items()
    ]
    rows += [
        {"idoft_row": n, "reason": reason, "stratum": walked.strata[n], "detail": detail}
        for n, (reason, detail) in walked.reasons.items()
    ]
    return sorted(rows, key=lambda row: row["idoft_row"])


def funnel_counts(rows: list[dict], not_accepted: int) -> dict:
    overall = Counter({"status_not_accepted": not_accepted})
    per_stratum: dict[str, Counter] = defaultdict(Counter)
    for row in rows:
        overall[row["reason"]] += 1
        if row["stratum"] is not None:
            per_stratum[row["stratum"]][row["reason"]] += 1
    return {
        "overall": dict(overall),
        "per_stratum": {name: dict(counts) for name, counts in per_stratum.items()},
    }


def write_version(
    directory: Path, drawn: list[Drawn], funnel: list[dict], manifest: dict
) -> dict[str, str]:
    """Writes the split, funnel, and manifest files and returns the SHA256 of each data file."""
    if os.path.lexists(directory):
        raise FileExistsError(f"corpus version already exists and is frozen: {directory}")
    by_row = sorted(drawn, key=lambda item: item.row.idoft_row)
    contents = {
        f"{split}.jsonl": _jsonl([item.to_json() for item in by_row if item.split == split])
        for split in SPLITS
    }
    contents["funnel.jsonl"] = _jsonl(funnel)
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    record = {**manifest, "files": hashes}
    text = json.dumps(record, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    contents["manifest.json"] = text.encode("utf-8")

    directory.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{directory.name}-", dir=directory.parent))
    for name, data in contents.items():
        (staging / name).write_bytes(data)
    os.chmod(staging, 0o755)
    if os.path.lexists(directory):
        raise FileExistsError(f"corpus version appeared while writing: {directory}")
    os.rename(staging, directory)
    return hashes
