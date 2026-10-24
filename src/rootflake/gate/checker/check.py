"""The shortcut checker: ADR 0007 Decisions 2 to 9.

It reads the Java files the patch validator approved, before and after the patch, and returns
reject, accept, or accept flagged, with a code, file and line for every finding. Any failure
inside it is a reject with a `checker/` code, never an accept.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rootflake.gate.checker.files import MAX_FILES, CheckerError, read_side
from rootflake.gate.checker.lexer import Unparsable, tokenize
from rootflake.gate.checker.syntax import CompilationUnit, parse


@dataclass(frozen=True)
class Finding:
    code: str
    path: str | None
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


@dataclass(frozen=True)
class FileChange:
    path: str
    before: CompilationUnit | None
    after: CompilationUnit


def _judge(
    changes: list[FileChange], target: Target | None, overridden: Sequence[str]
) -> list[Finding]:
    return []


def _read(paths: Sequence[str], pristine: Path, workspace: Path):
    findings: list[Finding] = []
    changes: list[FileChange] = []
    for path in paths:
        if not path.endswith(".java"):
            continue
        try:
            before, after = read_side(pristine, path), read_side(workspace, path)
        except CheckerError:
            findings.append(Finding("checker/checker_error", path))
            continue
        if before is None and after is None:
            findings.append(Finding("checker/checker_error", path))
        elif after is None:
            findings.append(Finding("disable/test_file_removed", path))
        else:
            try:
                old = None if before is None else parse(tokenize(before))
                changes.append(FileChange(path, old, parse(tokenize(after))))
            except Unparsable as error:
                findings.append(Finding("checker/unparsable", path, error.line))
    return findings, changes


def check(
    paths: Sequence[str],
    pristine: Path,
    workspace: Path,
    target: Target | None,
    overridden: Sequence[str] = (),
) -> CheckResult:
    try:
        if len(paths) > MAX_FILES:
            return CheckResult((Finding("checker/checker_error", None),))
        findings, changes = _read(paths, pristine, workspace)
        if any(f.code.startswith("checker/") for f in findings):
            return CheckResult(tuple(findings))
        return CheckResult(tuple(findings + _judge(changes, target, overridden)))
    except Exception:
        return CheckResult((Finding("checker/checker_error", None),))
