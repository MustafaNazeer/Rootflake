"""The shortcut checker: ADR 0007 Decisions 2 to 9.

It reads the Java files the patch validator approved, before and after the patch, and returns
reject, accept, or accept flagged, with a code, file and line for every finding. Any failure
inside it is a reject with a `checker/` code, never an accept.
"""

import re
from collections.abc import Sequence
from pathlib import Path

from rootflake.gate.checker import (
    assertions,
    flow,
    production,
    retries,
    survive,
    swallow,
    timing,
)
from rootflake.gate.checker.files import MAX_FILES, CheckerError, extension_candidates, read_side
from rootflake.gate.checker.lexer import Unparsable, tokenize
from rootflake.gate.checker.model import CheckResult, Context, FileChange, Finding, Target
from rootflake.gate.checker.syntax import parse
from rootflake.gate.validator import existing_java_problem, path_syntax

__all__ = [
    "CheckResult",
    "FileChange",
    "Finding",
    "Target",
    "check",
    "checked_target",
    "target_paths",
]
RULES = (survive, assertions, timing, flow, retries, swallow, production)
_IDENT = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")
_REFUSED = frozenset(["symlink", "not_regular", "mode"])
_ROOTS = (["src", "test", "java"], ["src", "main", "java"])
_CLASS = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
_NESTED = re.compile(r"(\$[A-Za-z0-9_]+)*")


def _module(path: str) -> str | None:
    """The module of a source path, before its first `src` component as the patch validator
    reads it, when that component starts a Java test or main root."""
    parts = path.split("/")
    if "src" not in parts:
        return None
    at = parts.index("src")
    return "/".join(parts[:at]) if parts[at : at + 3] in _ROOTS else None


def checked_target(target: Target | None) -> bool:
    """Whether a target is well formed: a dotted class name with optional nested parts, a method
    given as text or none, and a module that is none, the repository root or a clean relative
    ASCII path."""
    if target is None:
        return True
    name, method, module = target.class_name, target.method, target.module
    if not isinstance(name, str) or not isinstance(method, str | None):
        return False
    top, _, nested = name.partition("$")
    if not _CLASS.fullmatch(top) or not _NESTED.fullmatch(f"${nested}" if nested else ""):
        return False
    if module is None or module == "":
        return True
    return isinstance(module, str) and module.isascii() and path_syntax(module) is None


def target_paths(paths: Sequence[str], target: Target | None) -> list[str]:
    """Where the target class's top level file sits, in its module when the caller names it and
    otherwise in the module of each approved path, when the patch does not touch it. Its unchanged
    assertions are then read, so a patch that changes only a constant they read is still seen."""
    if target is None:
        return []
    if not checked_target(target):
        return []
    top = target.class_name.split("$", 1)[0]
    modules = {target.module} if target.module is not None else {_module(p) for p in paths} - {None}
    rel = "src/test/java/" + top.replace(".", "/") + ".java"
    found = {f"{m}/{rel}" if m else rel for m in modules}
    return sorted(found - set(paths))


def _judge(context: Context) -> list[Finding]:
    return [finding for rule in RULES for finding in rule.findings(context)]


def _extensions(context: Context, pristine: Path, room: int) -> list[Finding]:
    """Read the one pristine file that may declare each newly referenced extension type
    (ADR 0007 Decision 2). A type that cannot be resolved is left to be flagged; a read that is
    needed but refused, or past the file bound, is a reject on the referencing path."""
    read: set[str] = {c.path for c in context.changes}
    for path, fqn in retries.requests(context):
        module = _module(path)
        if module is None or not all(_IDENT.fullmatch(part) for part in fqn.split(".")):
            context.ambiguous.add(fqn)  # never read, so never taken as absent
            continue
        present, refused = [], False
        for candidate in extension_candidates(module, fqn):
            problem = existing_java_problem(candidate, pristine)
            if problem is None:
                present.append(candidate)
            elif problem.code in _REFUSED:
                return [Finding("checker/checker_error", path)]
            elif problem.code != "not_found":
                refused = True
        if refused or len(present) > 1:
            context.ambiguous.add(fqn)  # refused by a path rule, or in both roots: flagged
            continue
        if not present:
            context.absent.add(fqn)
            continue
        if present[0] in read:
            continue
        if room <= 0:
            return [Finding("checker/checker_error", path)]
        room -= 1
        read.add(present[0])
        try:
            content = read_side(pristine, present[0])
        except CheckerError:
            return [Finding("checker/checker_error", path)]
        if content is None:
            return [Finding("checker/checker_error", path)]  # there at the check, gone at the read
        try:
            unit = parse(tokenize(content))
        except Unparsable:
            return [Finding("checker/unparsable", path)]
        package, _, name = fqn.rpartition(".")
        if unit.package != (package or None) or not any(t.name == name for t in unit.types):
            context.ambiguous.add(fqn)
            continue
        context.extensions[present[0]] = unit
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


def _target_files(paths: Sequence[str], pristine: Path, target: Target | None):
    """The target files to read beside the approved paths, and a finding for each that is
    refused, or that is missing from the module the caller named."""
    extra, refused = [], []
    for path in target_paths(paths, target):
        try:
            present = read_side(pristine, path) is not None
        except CheckerError:
            refused.append(Finding("checker/checker_error", path))
            continue
        if present:
            extra.append(path)
        elif target is not None and target.module is not None:
            refused.append(Finding("checker/checker_error", path))  # the named module lacks it
    return extra, refused


def check(
    paths: Sequence[str],
    pristine: Path,
    workspace: Path,
    target: Target | None,
    overridden: Sequence[str] = (),
) -> CheckResult:
    try:
        if len(paths) > MAX_FILES or not checked_target(target):
            return CheckResult((Finding("checker/checker_error", None),))
        extra, missing = _target_files(paths, pristine, target)
        if missing:
            return CheckResult(tuple(missing))
        if len(paths) + len(extra) > MAX_FILES:
            return CheckResult((Finding("checker/checker_error", None),))
        findings, changes = _read([*paths, *extra], pristine, workspace)
        if any(f.code.startswith("checker/") for f in findings):
            return CheckResult(tuple(findings))
        context = Context(changes, target, overridden, paths=tuple(paths))
        refused = _extensions(context, pristine, MAX_FILES - len(paths) - len(extra))
        if refused:
            return CheckResult(tuple(refused))
        found = findings + _judge(context)
        if any(not finding.is_flag for finding in found):
            found = [finding for finding in found if not finding.is_flag]  # a reject beats a flag
        return CheckResult(tuple(found))
    except Unparsable as error:
        return CheckResult((Finding("checker/unparsable", None, error.line),))
    except Exception:
        return CheckResult((Finding("checker/checker_error", None),))
