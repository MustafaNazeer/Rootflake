"""The shortcut checker: ADR 0007 Decisions 2 to 9.

It reads the Java files the patch validator approved, before and after the patch, and returns
reject, accept, or accept flagged, with a code, file and line for every finding.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    line: int | None = None

    @property
    def is_flag(self) -> bool:
        return self.code.startswith("flag/")


@dataclass(frozen=True)
class Target:
    class_name: str
    method: str | None


@dataclass(frozen=True)
class CheckResult:
    findings: tuple[Finding, ...]

    @property
    def verdict(self) -> Literal["reject", "accept", "accept_flagged"]:
        if any(not finding.is_flag for finding in self.findings):
            return "reject"
        return "accept_flagged" if self.findings else "accept"


def check(
    paths: Sequence[str],
    pristine: Path,
    workspace: Path,
    target: Target | None,
    overridden: Sequence[str] = (),
) -> CheckResult:
    return CheckResult(())
