"""Runs the patch validator and the shortcut checker in a child process under limits.

ADR 0007 Decision 1: a crash, a hang or a limit hit inside either step must end as a reject,
never as an accept and never as an infrastructure retry. The child reads its request from a
private temporary folder and writes its result there, because a validation result can carry up
to twenty whole files, far more than the captured output keeps. The validator child loads the
override list from the package itself; its request has no field that could name another one.
The parent decodes every result strictly and refuses a finding on a path it did not ask about.
"""

import base64
import binascii
import json
import resource
import signal
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from rootflake.gate.checker.check import (
    CheckResult,
    Finding,
    Target,
    check,
    checked_target,
    target_paths,
)
from rootflake.gate.validator import Change, Rejection, Validation, path_syntax, validate
from rootflake.sandbox.executor import DockerRunner, ProcessRunner

CPU_LIMIT_S = 30
ADDRESS_SPACE_BYTES = 1 << 30
WALL_LIMIT_S = 60
RESULT_LIMIT_BYTES = 256 << 20
# A check result holds only codes, paths and lines, a few kilobytes for a real patch.
CHECK_RESULT_LIMIT_BYTES = 4 << 20
DEFAULT_RUNNER = DockerRunner()
_RESULT_KEYS = ["changes", "overridden", "rejections"]
_CODE_TYPES = frozenset(
    ["disable", "retry", "weaken_assertion", "swallow_failure", "checker", "flag"]
)


def apply_limits(cpu_s: int = CPU_LIMIT_S, address_space: int = ADDRESS_SPACE_BYTES) -> None:
    # Linux sends SIGXCPU at the soft limit and SIGKILL at the hard one; a kill at an equal
    # hard limit would be indistinguishable from an out of memory kill.
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s + 1))
    resource.setrlimit(resource.RLIMIT_AS, (address_space, address_space))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def _encode(validation: Validation) -> bytes:
    return json.dumps(
        {
            "rejections": [[r.code, r.path] for r in validation.rejections],
            "changes": [
                [c.path, c.created, base64.b64encode(c.content).decode()]
                for c in validation.changes
            ],
            "overridden": [[r.code, r.path] for r in validation.overridden],
        }
    ).encode()


def _rejections(items: object) -> tuple[Rejection, ...]:
    if not isinstance(items, list):
        raise ValueError("not a list")
    out = []
    for item in items:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not (item[1] is None or isinstance(item[1], str))
        ):
            raise ValueError(f"bad rejection {item!r}")
        out.append(Rejection(item[0], item[1]))
    return tuple(out)


def _changes(items: object) -> tuple[Change, ...]:
    if not isinstance(items, list):
        raise ValueError("not a list")
    out = []
    for item in items:
        if (
            not isinstance(item, list)
            or len(item) != 3
            or not isinstance(item[0], str)
            or not isinstance(item[1], bool)
            or not isinstance(item[2], str)
        ):
            raise ValueError(f"bad change {item!r}")
        out.append(Change(item[0], item[1], base64.b64decode(item[2], validate=True)))
    return tuple(out)


def _decode(data: bytes) -> Validation:
    result = json.loads(data)
    if not isinstance(result, dict) or sorted(result) != _RESULT_KEYS:
        raise ValueError("bad result object")
    validation = Validation(
        _rejections(result["rejections"]),
        _changes(result["changes"]),
        _rejections(result["overridden"]),
    )
    if validation.rejections and validation.changes:
        raise ValueError("a rejected result carries changes")
    for item in validation.changes + validation.overridden:
        if not isinstance(item.path, str) or path_syntax(item.path):
            raise ValueError(f"a result path fails the syntax rule: {item.path!r}")
    return validation


def _read_result(path: Path, limit: int = RESULT_LIMIT_BYTES) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError("result too large")
    return data


def _run_child(step: str, work: Path, runner: ProcessRunner, limit: int = RESULT_LIMIT_BYTES):
    args = [sys.executable, "-I", "-m", "rootflake.gate.isolated", step, str(work)]
    proc = runner.run(args, WALL_LIMIT_S)
    if proc.timed_out or proc.exit_code == -signal.SIGXCPU:
        return "timeout"
    if proc.exit_code != 0:
        return "error"
    return _read_result(work / "result.json", limit)


def validate_isolated(
    patch: bytes,
    pristine: Path,
    project: str,
    test: str,
    runner: ProcessRunner = DEFAULT_RUNNER,
) -> Validation:
    with tempfile.TemporaryDirectory(prefix="rootflake-validate-") as folder:
        work = Path(folder)
        (work / "patch.diff").write_bytes(patch)
        request = {"pristine": str(pristine), "project": project, "test": test}
        (work / "request.json").write_text(json.dumps(request))
        try:
            outcome = _run_child("validate", work, runner)
            if outcome == "timeout":
                return Validation((Rejection("validator_timeout"),))
            if outcome == "error":
                return Validation((Rejection("validator_error"),))
            return _decode(outcome)
        except OSError, ValueError, RecursionError, binascii.Error:
            return Validation((Rejection("validator_error"),))


def _decode_findings(data: bytes, paths: Sequence[str]) -> CheckResult:
    result = json.loads(data)
    if not isinstance(result, dict) or sorted(result) != ["findings"]:
        raise ValueError("bad result object")
    if not isinstance(result["findings"], list):
        raise ValueError("findings is not a list")
    allowed = set(paths)
    out = []
    for item in result["findings"]:
        if not isinstance(item, list) or len(item) != 3:
            raise ValueError(f"bad finding {item!r}")
        code, path, line = item
        if not isinstance(code, str) or code.split("/", 1)[0] not in _CODE_TYPES:
            raise ValueError(f"bad code {code!r}")
        if path is not None and path not in allowed:
            raise ValueError(f"finding names a path outside the check: {path!r}")
        if line is not None and (not isinstance(line, int) or isinstance(line, bool) or line < 1):
            raise ValueError(f"bad line {line!r}")
        out.append(Finding(code, path, line))
    return CheckResult(tuple(out))


def check_isolated(
    paths: Sequence[str],
    pristine: Path,
    workspace: Path,
    target: Target | None,
    overridden: Sequence[str] = (),
    runner: ProcessRunner = DEFAULT_RUNNER,
) -> CheckResult:
    error = CheckResult((Finding("checker/checker_error", None),))
    if not checked_target(target):
        return error
    with tempfile.TemporaryDirectory(prefix="rootflake-check-") as folder:
        work = Path(folder)
        request = {
            "paths": list(paths),
            "pristine": str(pristine),
            "workspace": str(workspace),
            "target": target
            and {"class": target.class_name, "method": target.method, "module": target.module},
            "overridden": list(overridden),
        }
        (work / "request.json").write_text(json.dumps(request))
        try:
            outcome = _run_child("check", work, runner, CHECK_RESULT_LIMIT_BYTES)
            if outcome == "timeout":
                return CheckResult((Finding("checker/checker_timeout", None),))
            if outcome == "error":
                return error
            return _decode_findings(outcome, [*paths, *target_paths(paths, target)])
        except OSError, ValueError, RecursionError:
            return error


def _main_validate(work: Path) -> None:
    request = json.loads((work / "request.json").read_text())
    validation = validate(
        (work / "patch.diff").read_bytes(),
        Path(request["pristine"]),
        request["project"],
        request["test"],
    )
    (work / "result.json").write_bytes(_encode(validation))


def _main_check(work: Path) -> None:
    request = json.loads((work / "request.json").read_text())
    found = request["target"]
    target = found and Target(found["class"], found["method"], found["module"])
    result = check(
        request["paths"],
        Path(request["pristine"]),
        Path(request["workspace"]),
        target,
        request["overridden"],
    )
    findings = [[f.code, f.path, f.line] for f in dict.fromkeys(result.findings)]
    (work / "result.json").write_bytes(json.dumps({"findings": findings}).encode())


def main(step: str, folder: str) -> None:
    apply_limits()
    {"validate": _main_validate, "check": _main_check}[step](Path(folder))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
