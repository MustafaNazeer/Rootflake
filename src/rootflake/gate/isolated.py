"""Runs the patch validator in a child process under CPU, address space and wall limits.

ADR 0007 Decision 1: a crash, a hang or a limit hit inside the validator must end as a
reject, never as an accept and never as an infrastructure retry. The child reads the patch and
the request from a private temporary folder and writes its result there, because a result can
carry up to twenty whole files, far more than the captured output keeps. The child loads the
override list from the package itself; the request has no field that could name another one.
"""

import base64
import binascii
import json
import resource
import signal
import sys
import tempfile
from pathlib import Path

from rootflake.gate.validator import Change, Rejection, Validation, validate
from rootflake.sandbox.executor import DockerRunner, ProcessRunner

CPU_LIMIT_S = 30
ADDRESS_SPACE_BYTES = 1 << 30
WALL_LIMIT_S = 60
RESULT_LIMIT_BYTES = 256 << 20
DEFAULT_RUNNER = DockerRunner()
_RESULT_KEYS = ["changes", "overridden", "rejections"]


def apply_limits(cpu_s: int = CPU_LIMIT_S, address_space: int = ADDRESS_SPACE_BYTES) -> None:
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
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
    return validation


def _read_result(path: Path) -> bytes:
    with path.open("rb") as handle:
        data = handle.read(RESULT_LIMIT_BYTES + 1)
    if len(data) > RESULT_LIMIT_BYTES:
        raise ValueError("result too large")
    return data


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
        args = [sys.executable, "-I", "-m", "rootflake.gate.isolated", str(work)]
        proc = runner.run(args, WALL_LIMIT_S)
        if proc.timed_out or proc.exit_code == -signal.SIGXCPU:
            return Validation((Rejection("validator_timeout"),))
        if proc.exit_code != 0:
            return Validation((Rejection("validator_error"),))
        try:
            return _decode(_read_result(work / "result.json"))
        except OSError, ValueError, RecursionError, binascii.Error:
            return Validation((Rejection("validator_error"),))


def main(folder: str) -> None:
    apply_limits()
    work = Path(folder)
    request = json.loads((work / "request.json").read_text())
    validation = validate(
        (work / "patch.diff").read_bytes(),
        Path(request["pristine"]),
        request["project"],
        request["test"],
    )
    (work / "result.json").write_bytes(_encode(validation))


if __name__ == "__main__":
    main(sys.argv[1])
