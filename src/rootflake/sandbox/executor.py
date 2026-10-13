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
HEAD_LIMIT = 16 * 1024
TAIL_LIMIT = 48 * 1024
OUTPUT_LIMIT = HEAD_LIMIT + TAIL_LIMIT
RETRIED = (Outcome.INFRASTRUCTURE, Outcome.TIMEOUT)

_CONTROL_RE = re.compile(
    r"\x1b\[[0-9;?]*[A-Za-z]"  # CSI
    r"|\x1b[\]PX^_][^\x07\x1b]{0,256}(?:\x07|\x1b\\)?"  # OSC, DCS, SOS, PM, APC
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]"  # C0, C1, bidi
)
TRUNCATED = "\n[truncated]\n"


@dataclass(frozen=True)
class ProcResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    timed_out: bool
    stdout_dropped: int = 0
    stderr_dropped: int = 0


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


def _clean(data: bytes) -> str:
    return _CONTROL_RE.sub("", data.decode("utf-8", errors="replace"))


def sanitize_output(
    data: bytes, dropped: int = 0, head: int = HEAD_LIMIT, tail: int = TAIL_LIMIT
) -> str:
    """Strip terminal controls, keeping at most the first `head` and last `tail` bytes.

    `dropped` says bytes were already cut between the head and the tail while draining.
    """
    if len(data) > head + tail:
        data, dropped = data[:head] + data[-tail:], dropped + len(data) - head - tail
    if dropped:
        return _clean(data[:head]) + TRUNCATED + _clean(data[head:])
    return _clean(data)


def result_output(result: ProcResult) -> str:
    return sanitize_output(result.stdout, result.stdout_dropped) + sanitize_output(
        result.stderr, result.stderr_dropped
    )


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
                output=result_output(result),
            )


class _Capture:
    """The first HEAD_LIMIT bytes of a stream and a rolling window of its last TAIL_LIMIT."""

    def __init__(self) -> None:
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def add(self, chunk: bytes) -> None:
        room = HEAD_LIMIT - len(self.head)
        self.head += chunk[:room]
        self.tail += chunk[room:] if room > 0 else chunk
        excess = len(self.tail) - TAIL_LIMIT
        if excess > 0:
            del self.tail[:excess]
            self.dropped += excess


def _drain(stream: IO[bytes], capture: _Capture) -> None:
    while chunk := stream.read(1 << 16):
        capture.add(chunk)


class DockerRunner:
    """Runs the checked `docker run` command as a subprocess.

    Each output pipe is drained on its own thread: the first HEAD_LIMIT and the last
    TAIL_LIMIT bytes are kept and the rest is read and discarded, so a test that prints
    without bound can neither exhaust host memory or disk nor block on a full pipe, and the
    end of the output, where a failure is usually reported, survives. Wall time bounds the
    total.
    """

    def run(self, args: list[str], timeout_s: float) -> ProcResult:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=PIPE, stderr=PIPE)
        out, err = _Capture(), _Capture()
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
        return ProcResult(
            code,
            bytes(out.head + out.tail),
            bytes(err.head + err.tail),
            timed_out,
            stdout_dropped=out.dropped,
            stderr_dropped=err.dropped,
        )

    def stop(self, name: str) -> None:
        for argv in (["docker", "stop", name], ["docker", "rm", "-f", name]):
            try:
                subprocess.run(argv, capture_output=True, timeout=60, check=False)
            except subprocess.TimeoutExpired:
                pass
