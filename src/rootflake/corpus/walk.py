"""The seeded ranked walk that draws the corpus (ADR 0006, Decision 4), rules 6 to 14.

Rules 6 to 8 are checked before any network or sandbox check, and rules 9 to 12 come from a
`Checks` object so the walk itself stays pure. A transient failure inside a check raises
`TransientError` and aborts the walk: recording it as a rule outcome would change the draw.
"""

import hashlib
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol

from rootflake.corpus.allocate import reallocate
from rootflake.corpus.funnel import Eligible
from rootflake.corpus.idoft import Row, derive_test_class

SUPPORTED_LEVELS = (8, 17)
PROJECT_CAP = 2


class TransientError(RuntimeError):
    """A failure that says nothing about the row, such as a rate limit or a network error."""


@dataclass(frozen=True)
class FixDiff:
    pr_head_sha: str
    pr_merge_commit_sha: str
    pr_merged_at: str
    fix_diff_sha256: str


@dataclass(frozen=True)
class Unavailable:
    detail: str


class Checks(Protocol):
    def fix_diff(self, row: Row) -> FixDiff | Unavailable: ...

    def java_level(self, row: Row) -> int | None: ...

    def build(self, row: Row) -> int | None: ...

    def runnable(self, row: Row, jdk: int) -> bool: ...


@dataclass(frozen=True)
class Drawn:
    row: Row
    stratum: str
    fix: FixDiff
    jdk: str
    rank_key: str
    split: str

    @property
    def class_key(self) -> tuple[str, str]:
        return (self.row.project_url.lower(), derive_test_class(self.row.test_name))

    def to_json(self) -> dict:
        return {
            "idoft_row": self.row.idoft_row,
            "project_url": self.row.project_url,
            "sha_detected": self.row.sha_detected,
            "module_path": self.row.module_path,
            "test_name": self.row.test_name,
            "test_class": derive_test_class(self.row.test_name),
            "category": self.row.category,
            "stratum": self.stratum,
            "pr_url": self.row.pr_url,
            "pr_head_sha": self.fix.pr_head_sha,
            "pr_merge_commit_sha": self.fix.pr_merge_commit_sha,
            "pr_merged_at": self.fix.pr_merged_at,
            "fix_diff_sha256": self.fix.fix_diff_sha256,
            "jdk": self.jdk,
            "rank_key": self.rank_key,
            "split": self.split,
        }


@dataclass
class WalkResult:
    drawn: list[Drawn] = field(default_factory=list)
    reasons: dict[int, tuple[str, str | None]] = field(default_factory=dict)
    strata: dict[str, str] = field(default_factory=dict)
    planned: dict[str, tuple[int, int]] = field(default_factory=dict)
    final: dict[str, tuple[int, int]] = field(default_factory=dict)
    shortfalls: list[dict] = field(default_factory=list)
    reallocations: list[dict] = field(default_factory=list)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def rank_key(seed: str, stratum: str, pr_url: str) -> str:
    return _sha256(f"{seed}\n{stratum}\n{pr_url}")


def row_key(seed: str, idoft_row: int) -> str:
    return _sha256(f"{seed}\n{idoft_row}")


def walk(
    eligible: list[Eligible],
    plan: dict[str, tuple[int, int]],
    seed: str,
    checks: Checks,
    supported: tuple[int, int] = SUPPORTED_LEVELS,
) -> WalkResult:
    units: dict[str, dict[str, list[Eligible]]] = defaultdict(lambda: defaultdict(list))
    for item in eligible:
        units[item.stratum][item.row.pr_url].append(item)
    if set(units) - set(plan):
        raise ValueError(f"strata without a plan: {sorted(set(units) - set(plan))}")

    result = WalkResult(planned=dict(plan))
    quotas = {name: list(plan[name]) for name in plan}
    order = sorted(plan, key=lambda name: (len(units[name]), name))
    claimed: set[str] = set()
    per_project: dict[tuple[str, str], int] = defaultdict(int)
    class_split: dict[tuple[str, str], str] = {}
    fixes: dict[str, FixDiff | Unavailable] = {}
    low, high = supported

    def record(item: Eligible, reason: str, detail: str | None = None) -> None:
        result.reasons[item.row.idoft_row] = (reason, detail)
        result.strata[item.row.idoft_row] = item.stratum

    def examine(item: Eligible, split: str) -> tuple[FixDiff, int] | None:
        if per_project[(item.stratum, item.project)] >= PROJECT_CAP:
            return record(item, "project_cap_reached")
        if class_split.get(item.class_key, split) != split:
            return record(item, "class_in_other_split")
        if item.pr not in fixes:
            fixes[item.pr] = checks.fix_diff(item.row)
        fix = fixes[item.pr]
        if isinstance(fix, Unavailable):
            return record(item, "fix_diff_unavailable", fix.detail)
        level = checks.java_level(item.row)
        if level is not None and not low <= level <= high:
            return record(item, "java_out_of_range", str(level))
        jdk = checks.build(item.row)
        if jdk is None:
            return record(item, "build_failed")
        if not checks.runnable(item.row, jdk):
            return record(item, "test_not_runnable", str(jdk))
        return fix, jdk

    for position, stratum in enumerate(order):
        ranked = sorted(units[stratum], key=lambda url: (rank_key(seed, stratum, url), url))
        dev_quota, test_quota = quotas[stratum]
        drawn = {"dev": 0, "test": 0}
        for url in ranked:
            rows = sorted(units[stratum][url], key=lambda e: row_key(seed, e.row.idoft_row))
            full = drawn["test"] >= test_quota and drawn["dev"] >= dev_quota
            if full:
                for item in rows:
                    record(item, "not_examined")
                continue
            if url.lower() in claimed:
                for item in rows:
                    record(item, "same_pr_already_drawn")
                continue
            split = "test" if drawn["test"] < test_quota else "dev"
            for item in rows:
                if url.lower() in claimed:
                    record(item, "same_pr_already_drawn")
                    continue
                passed = examine(item, split)
                if passed is None:
                    continue
                fix, jdk = passed
                key = rank_key(seed, stratum, url)
                result.drawn.append(Drawn(item.row, stratum, fix, str(jdk), key, split))
                record(item, "drawn", str(jdk))
                claimed.add(url.lower())
                per_project[(stratum, item.project)] += 1
                class_split[item.class_key] = split
                drawn[split] += 1

        result.final[stratum] = (drawn["dev"], drawn["test"])
        lost_test, lost_dev = test_quota - drawn["test"], dev_quota - drawn["dev"]
        if not (lost_test or lost_dev):
            continue
        result.shortfalls.append({"stratum": stratum, "lost_test": lost_test, "lost_dev": lost_dev})
        remaining = {
            later: sum(1 for url in units[later] if url.lower() not in claimed)
            for later in order[position + 1 :]
        }
        remaining = {name: count for name, count in remaining.items() if count}
        if not remaining:
            continue
        extra_test = reallocate(lost_test, remaining)
        extra_dev = reallocate(lost_dev, remaining)
        for name in remaining:
            quotas[name][0] += extra_dev[name]
            quotas[name][1] += extra_test[name]
        result.reallocations.append(
            {
                "from": stratum,
                "test": {k: v for k, v in extra_test.items() if v},
                "dev": {k: v for k, v in extra_dev.items() if v},
            }
        )
    return result
