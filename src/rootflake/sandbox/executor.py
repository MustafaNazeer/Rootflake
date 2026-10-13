"""Runs one test in the sandbox and records its outcome.

A timeout or an infrastructure result is retried once and then recorded as is, never as a
failed fix (SH-10). Captured output is capped and stripped of terminal controls (SH-17).
"""

import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from subprocess import PIPE
from typing import IO, Protocol

from rootflake.sandbox.container import RunSpec, SandboxLimits, build_docker_args, check_args
from rootflake.sandbox.results import Outcome, TestId, classify, maven_test_command, parse_report

MAX_TIMEOUT_S = 6 * 3600
OUTPUT_LIMIT = 64 * 1024
RETRIED = (Outcome.INFRASTRUCTURE, Outcome.TIMEOUT)

_CONTROL_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|[\x00-\x08\x0b-\x1f\x7f]")


@dataclass(frozen=True)
class ProcResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool


class ProcessRunner(Protocol):
    def run(self, args: list[str], timeout_s: float) -> ProcResult: ...

    def stop(self, name: str) -> None: ...


@dataclass(frozen=True)
class RunRecord:
    test: str
    outcome: Outcome
    attempts: int
    exit_code: int
    report: str
    seconds: float
    output: str


def sanitize_output(data: bytes, limit: int = OUTPUT_LIMIT) -> str:
    text = _CONTROL_RE.sub("", data.decode("utf-8", errors="replace"))
    if len(text) > limit:
        return text[:limit] + "\n[truncated]"
    return text


def report_path(module: str, test: TestId) -> Path:
    base = Path(module) if module else Path()
    return base / "target" / "surefire-reports" / f"TEST-{test.class_name}.xml"


def _container_name() -> str:
    return f"rootflake-{secrets.token_hex(8)}"


def run_test(
    spec: RunSpec,
    limits: SandboxLimits,
    test: TestId,
    module: str,
    runner: ProcessRunner,
    read_report: Callable[[Path, Path], bytes | None],
    allowed_roots: Sequence[Path],
    timeout_s: float,
) -> RunRecord:
    if not 0 < timeout_s <= MAX_TIMEOUT_S:
        raise ValueError(f"timeout must be in (0, {MAX_TIMEOUT_S}] seconds, got {timeout_s}")
    command = maven_test_command(test, module)
    attempts = 0
    while True:
        attempts += 1
        attempt = replace(spec, command=command, name=_container_name())
        args = build_docker_args(attempt, limits)
        check_args(args, allowed_roots, spec.image)
        started = time.monotonic()
        result = runner.run(args, timeout_s)
        seconds = time.monotonic() - started
        if result.timed_out:
            runner.stop(attempt.name)
        data = read_report(spec.workspace, report_path(module, test))
        report = "missing" if data is None else parse_report(data, test)
        outcome = classify(result.exit_code, report, result.timed_out)
        if outcome not in RETRIED or attempts == 2:
            return RunRecord(
                test=str(test),
                outcome=outcome,
                attempts=attempts,
                exit_code=result.exit_code,
                report=report,
                seconds=seconds,
                output=sanitize_output(result.stdout + result.stderr),
            )


def _drain(stream: IO[bytes], keep: bytearray) -> None:
    while chunk := stream.read(1 << 16):
        room = OUTPUT_LIMIT + 1 - len(keep)
        if room > 0:
            keep += chunk[:room]


class DockerRunner:
    """Runs the checked `docker run` command as a subprocess.

    Each output pipe is drained on its own thread: the first OUTPUT_LIMIT + 1 bytes are kept
    and the rest is read and discarded, so a test that prints without bound can neither
    exhaust host memory or disk nor block on a full pipe. Wall time bounds the total.
    """

    def run(self, args: list[str], timeout_s: float) -> ProcResult:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=PIPE, stderr=PIPE)
        out, err = bytearray(), bytearray()
        readers = [
            threading.Thread(target=_drain, args=(process.stdout, out), daemon=True),
            threading.Thread(target=_drain, args=(process.stderr, err), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            code, timed_out = process.wait(timeout=timeout_s), False
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            code, timed_out = -1, True
        for reader in readers:
            reader.join(timeout=10)
        return ProcResult(code, bytes(out), bytes(err), timed_out)

    def stop(self, name: str) -> None:
        subprocess.run(["docker", "stop", name], capture_output=True, timeout=60, check=False)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False)
