"""Rules 1 to 5 of the corpus funnel (ADR 0006, Decision 3), applied to every row.

GitHub owner and repository names are case insensitive, so project and PR identity is
compared case folded; the verbatim strings are kept for the files and the rank key.
"""

import re
from collections import defaultdict
from dataclasses import dataclass

from rootflake.corpus.idoft import Row, derive_test_class

REASONS = (
    "status_not_accepted",
    "duplicate_test",
    "multiple_labels",
    "category_excluded",
    "stratum_too_thin",
    "same_pr_already_drawn",
    "project_cap_reached",
    "class_in_other_split",
    "fix_diff_unavailable",
    "java_out_of_range",
    "build_failed",
    "test_not_runnable",
    "not_examined",
    "drawn",
)
FOLD = {
    "ID": "ID",
    "OD": "OD",
    "OD-Vic": "OD",
    "OD-Brit": "OD",
    "NIO": "NIO",
    "NOD": "NOD",
    "TZD": "TZD",
}
INCLUDED = frozenset(FOLD)
EXCLUDED = ("ID-HtF", "NDOD", "NDOI", "OSD", "TD", "UD")
MIN_STRATUM_PRS = 6

_PROJECT_RE = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)")
_PR_RE = re.compile(r"(https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9._-]+)/pull/([1-9][0-9]*)")
_SHA_RE = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class ScreenResult:
    eligible: list[Eligible]
    reasons: dict[int, str]
    strata: dict[int, str]
    not_accepted: int
    accepted: frozenset[int]


@dataclass(frozen=True)
class Eligible:
    row: Row
    stratum: str

    @property
    def project(self) -> str:
        return self.row.project_url.lower()

    @property
    def pr(self) -> str:
        return self.row.pr_url.lower()

    @property
    def class_key(self) -> tuple[str, str]:
        return (self.project, derive_test_class(self.row.test_name))


def _check(row: Row) -> None:
    def bad(why: str) -> ValueError:
        return ValueError(f"row {row.idoft_row} is malformed: {why}")

    if not _PROJECT_RE.fullmatch(row.project_url):
        raise bad(f"project URL {row.project_url!r}")
    if not _SHA_RE.fullmatch(row.sha_detected):
        raise bad(f"SHA Detected {row.sha_detected!r}")
    match = _PR_RE.fullmatch(row.pr_url)
    if not match or match.group(1).lower() != row.project_url.lower():
        raise bad(f"PR link {row.pr_url!r} is not a pull request of {row.project_url!r}")
    if not row.category:
        raise bad("empty Category")
    unknown = [
        label for label in row.category.split(";") if label not in FOLD and label not in EXCLUDED
    ]
    if unknown:
        raise bad(f"unknown IDoFT label {unknown[0]!r}")
    try:
        derive_test_class(row.test_name)
    except ValueError as error:
        raise bad(str(error)) from error


def screen(rows: list[Row]) -> ScreenResult:
    """Rules 1 to 5: the eligible rows, the reason and (from rule 5) stratum of each removed row."""
    accepted = [row for row in rows if row.status == "Accepted"]
    spellings: dict[str, set[str]] = defaultdict(set)
    for row in accepted:
        _check(row)
        spellings[row.pr_url.lower()].add(row.pr_url)
    for variants in spellings.values():
        if len(variants) > 1:
            raise ValueError(
                f"one pull request is spelled {len(variants)} ways: {sorted(variants)}"
            )

    copies: dict[tuple[str, str, str], int] = defaultdict(int)
    for row in accepted:
        copies[(row.project_url.lower(), row.module_path, row.test_name)] += 1

    reasons: dict[int, str] = {}
    survivors: list[Eligible] = []
    for row in accepted:
        if copies[(row.project_url.lower(), row.module_path, row.test_name)] > 1:
            reasons[row.idoft_row] = "duplicate_test"
        elif ";" in row.category:
            reasons[row.idoft_row] = "multiple_labels"
        elif row.category not in INCLUDED:
            reasons[row.idoft_row] = "category_excluded"
        else:
            survivors.append(Eligible(row, FOLD[row.category]))

    prs: dict[str, set[str]] = defaultdict(set)
    for item in survivors:
        prs[item.stratum].add(item.pr)
    eligible = []
    strata: dict[int, str] = {}
    for item in survivors:
        if len(prs[item.stratum]) < MIN_STRATUM_PRS:
            reasons[item.row.idoft_row] = "stratum_too_thin"
            strata[item.row.idoft_row] = item.stratum
        else:
            eligible.append(item)
    ids = frozenset(row.idoft_row for row in accepted)
    return ScreenResult(eligible, reasons, strata, len(rows) - len(accepted), ids)
