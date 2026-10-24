"""The shortcut checker: ADR 0007 Decisions 2 to 9.

It reads the Java files the patch validator approved, before and after the patch, and returns
reject, accept, or accept flagged, with a code, file and line for every finding. Any failure
inside it is a reject with a `checker/` code, never an accept.
"""

from collections.abc import Sequence
from pathlib import Path

from rootflake.gate.checker import assertions, survive
from rootflake.gate.checker.files import MAX_FILES, CheckerError, read_side
from rootflake.gate.checker.lexer import Unparsable, tokenize
from rootflake.gate.checker.model import CheckResult, Context, FileChange, Finding, Target
from rootflake.gate.checker.syntax import parse

__all__ = ["CheckResult", "FileChange", "Finding", "Target", "check"]
RULES = (survive, assertions)


def _judge(
    changes: list[FileChange], target: Target | None, overridden: Sequence[str]
) -> list[Finding]:
    context = Context(changes, target, overridden)
    return [finding for rule in RULES for finding in rule.findings(context)]


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
        found = findings + _judge(changes, target, overridden)
        if any(not finding.is_flag for finding in found):
            found = [finding for finding in found if not finding.is_flag]  # a reject beats a flag
        return CheckResult(tuple(found))
    except Unparsable as error:
        return CheckResult((Finding("checker/unparsable", None, error.line),))
    except Exception:
        return CheckResult((Finding("checker/checker_error", None),))
