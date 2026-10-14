import subprocess
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import pytest

from rootflake.sandbox import executor
from rootflake.sandbox.container import RunSpec, SandboxLimits
from rootflake.sandbox.executor import (
    HEAD_LIMIT,
    OUTPUT_LIMIT,
    TAIL_LIMIT,
    DockerRunner,
    ProcResult,
    report_path,
    run_test,
    sanitize_output,
)
from rootflake.sandbox.reports import read_report
from rootflake.sandbox.results import Outcome, TestId

IMAGE = "rootflake-sandbox@sha256:" + "a" * 64
ROOT = Path(tempfile.mkdtemp(prefix="rootflake-exec-")).resolve()
(ROOT / "ws").mkdir()
(ROOT / "m2").mkdir()
ROOTS = (ROOT,)
TEST = TestId.parse("demo.DemoTest#adds")
PASS = b'<testsuite><testcase classname="demo.DemoTest" name="adds"/></testsuite>'
FAIL = (
    b'<testsuite><testcase classname="demo.DemoTest" name="adds"><failure/></testcase></testsuite>'
)


def base_spec(name="rootflake-00000000"):
    return RunSpec(
        image=IMAGE,
        workspace=ROOT / "ws",
        maven_repo=ROOT / "m2",
        jdk=17,
        command=["placeholder"],
        name=name,
    )


class FakeRunner:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.stopped = []

    def run(self, args, timeout_s):
        self.calls.append((args, timeout_s))
        return self.script.pop(0)

    def stop(self, name):
        self.stopped.append(name)


def reports(*contents):
    queue = list(contents)
    return lambda workspace, relative, stale=None: queue.pop(0)


def ok(code=0):
    return ProcResult(exit_code=code, stdout=b"", stderr=b"", timed_out=False)


def go(runner, read, **kw):
    return run_test(
        base_spec(), SandboxLimits(), TEST, "core", runner, read, ROOTS, timeout_s=300, **kw
    )


def test_a_passing_run_takes_one_attempt():
    runner = FakeRunner([ok()])
    record = go(runner, reports(PASS))
    assert record.outcome is Outcome.PASSED
    assert record.attempts == 1


def test_the_command_is_the_maven_test_command_and_passes_the_check():
    runner = FakeRunner([ok()])
    go(runner, reports(PASS))
    args = runner.calls[0][0]
    image_at = args.index(IMAGE)
    assert args[image_at + 1 :] == [
        "mvn",
        "surefire:test",
        "-Dtest=demo.DemoTest#adds",
        "-pl",
        "core",
    ]


def test_a_failing_run_is_not_retried():
    runner = FakeRunner([ok(1)])
    record = go(runner, reports(FAIL))
    assert (record.outcome, record.attempts) == (Outcome.FAILED, 1)


def test_an_infrastructure_result_is_retried_once():
    runner = FakeRunner([ok(1), ok()])
    record = go(runner, reports(PASS, PASS))
    assert (record.outcome, record.attempts) == (Outcome.PASSED, 2)


def test_two_infrastructure_results_end_as_infrastructure():
    runner = FakeRunner([ok(137), ok(137)])
    record = go(runner, reports(PASS, PASS))
    assert (record.outcome, record.attempts) == (Outcome.INFRASTRUCTURE, 2)


def test_a_timeout_stops_the_container_and_is_retried_once():
    timed_out = ProcResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True)
    runner = FakeRunner([timed_out, timed_out])
    record = go(runner, reports(None, None))
    assert (record.outcome, record.attempts) == (Outcome.TIMEOUT, 2)
    assert len(runner.stopped) == 2


def test_each_attempt_uses_a_fresh_container_name():
    runner = FakeRunner([ok(137), ok()])
    go(runner, reports(PASS, PASS))
    names = [c[0][c[0].index("--name") + 1] for c in runner.calls]
    assert len(set(names)) == 2


def test_missing_report_file_is_infrastructure():
    runner = FakeRunner([ok(), ok()])
    record = go(runner, reports(None, None))
    assert record.outcome is Outcome.INFRASTRUCTURE


def test_report_path_is_inside_the_module_reports_directory():
    path = report_path("core", TEST)
    assert path == Path("core/target/surefire-reports/TEST-demo.DemoTest.xml")
    assert report_path("", TEST) == Path("target/surefire-reports/TEST-demo.DemoTest.xml")


def test_output_keeps_the_head_and_the_tail_and_strips_terminal_controls():
    data = b"ok\x1b[31mred\x1b[0m" + b"x" * 100 + b"\x07end\x1b[0m!"
    text = sanitize_output(data, head=14, tail=9)
    assert text == "okred\n[truncated]\nend!"


def test_output_within_the_limits_is_kept_whole():
    assert sanitize_output(b"a" * 10 + b"b" * 10, head=10, tail=10) == "a" * 10 + "b" * 10


def test_dropped_bytes_put_the_marker_at_the_head_boundary():
    assert sanitize_output(b"headtail", dropped=5, head=4, tail=4) == "head\n[truncated]\ntail"


def test_the_default_limits_are_sixteen_and_forty_eight_kib():
    assert (HEAD_LIMIT, TAIL_LIMIT, OUTPUT_LIMIT) == (16 * 1024, 48 * 1024, 64 * 1024)


def test_invalid_utf8_output_does_not_raise():
    assert sanitize_output(b"\xff\xfe ok").endswith("ok")


def test_record_output_marks_a_stream_that_lost_bytes():
    dropped = ProcResult(1, b"x" * HEAD_LIMIT + b"last line", b"", False, stdout_dropped=99)
    record = go(FakeRunner([dropped]), reports(FAIL))
    assert record.output == "x" * HEAD_LIMIT + "\n[truncated]\nlast line"


def test_capture_keeps_a_bounded_head_and_rolling_tail():
    capture = executor._Capture()
    for i in range(64):
        capture.add(bytes([65 + i % 26]) * 65536)
        assert len(capture.head) <= HEAD_LIMIT and len(capture.tail) <= TAIL_LIMIT
    assert capture.head == b"A" * HEAD_LIMIT
    assert capture.tail == b"L" * TAIL_LIMIT
    assert capture.dropped == 64 * 65536 - OUTPUT_LIMIT


@pytest.mark.parametrize("timeout_s", [0, -1, 7 * 3600])
def test_timeout_must_be_bounded(timeout_s):
    with pytest.raises(ValueError):
        run_test(
            base_spec(),
            SandboxLimits(),
            TEST,
            "",
            FakeRunner([]),
            reports(),
            ROOTS,
            timeout_s=timeout_s,
        )


def test_docker_runner_keeps_the_head_and_tail_of_a_noisy_process():
    script = (
        "import sys\nsys.stdout.write('START')\n"
        "for _ in range(4000): sys.stdout.write('x' * 4096)\nsys.stdout.write('END')"
    )
    result = DockerRunner().run(["python3", "-c", script], timeout_s=60)
    assert result.exit_code == 0
    assert len(result.stdout) == OUTPUT_LIMIT
    assert result.stdout.startswith(b"START") and result.stdout.endswith(b"END")
    assert result.stdout_dropped == 5 + 4000 * 4096 + 3 - OUTPUT_LIMIT
    assert result.stderr_dropped == 0


def test_docker_runner_reports_a_timeout():
    result = DockerRunner().run(["sleep", "5"], timeout_s=0.5)
    assert result.timed_out


def test_docker_runner_kills_a_process_that_outlives_its_timeout():
    started = time.monotonic()
    result = DockerRunner().run(["python3", "-c", "import time; time.sleep(30)"], timeout_s=0.5)
    assert result.timed_out
    assert time.monotonic() - started < 5


def test_stop_survives_a_hung_docker_daemon(monkeypatch):
    calls = []

    def hung(argv, **kwargs):
        calls.append(argv[:2])
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(executor.subprocess, "run", hung)
    DockerRunner().stop("rootflake-0000000000000000")
    assert calls == [["docker", "stop"], ["docker", "rm"]]


@pytest.mark.parametrize(
    "raw",
    [
        "\u009b31mred",
        "\u009d0;title\u009c",
        "a\u202eb",
        "a\u2066b\u2069",
        "a\u200eb\u200fc\u061cd",
        "\x1b]0;title\x1b\\",
        "\x1bPpayload\x1b\\",
    ],
)
def test_c1_controls_and_bidi_marks_are_stripped(raw):
    text = sanitize_output(raw.encode("utf-8"))
    banned = "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f\u061c\x1b"
    assert not any(0x80 <= ord(c) <= 0x9F or c in banned for c in text)


def test_an_unterminated_string_control_cannot_erase_later_output():
    text = sanitize_output(("\x1b]" + "a" * 300 + "visible").encode())
    assert text.endswith("visible")


def test_run_test_checks_the_arguments_before_running_anything():
    other_root = Path(tempfile.mkdtemp(prefix="rootflake-other-")).resolve()
    runner = FakeRunner([ok()])
    with pytest.raises(ValueError):
        run_test(
            base_spec(),
            SandboxLimits(),
            TEST,
            "",
            runner,
            reports(PASS),
            (other_root,),
            timeout_s=300,
        )
    assert runner.calls == []


class WritingRunner(FakeRunner):
    """Writes the report an attempt would leave, or leaves the workspace untouched on None."""

    def __init__(self, report, script):
        super().__init__([result for result, _ in script])
        self.report = report
        self.writes = [content for _, content in script]

    def run(self, args, timeout_s):
        content = self.writes.pop(0)
        if content is not None:
            self.report.unlink(missing_ok=True)
            self.report.write_bytes(content)
        return super().run(args, timeout_s)


def run_real(tmp_path, stale, script):
    root = tmp_path.resolve()
    (root / "ws").mkdir()
    (root / "m2").mkdir()
    report = root / "ws" / report_path("core", TEST)
    report.parent.mkdir(parents=True)
    if stale is not None:
        report.write_bytes(stale)
    spec = replace(base_spec(), workspace=root / "ws", maven_repo=root / "m2")
    runner = WritingRunner(report, script)
    return run_test(spec, SandboxLimits(), TEST, "core", runner, read_report, (root,), 300)


def test_a_stale_passing_report_is_not_a_pass(tmp_path):
    record = run_real(tmp_path, PASS, [(ok(0), None), (ok(0), None)])
    assert (record.outcome, record.attempts, record.report) == (
        Outcome.INFRASTRUCTURE,
        2,
        "missing",
    )


def test_a_stale_failing_report_is_not_a_failure(tmp_path):
    record = run_real(tmp_path, FAIL, [(ok(1), None), (ok(1), None)])
    assert (record.outcome, record.attempts) == (Outcome.INFRASTRUCTURE, 2)


def test_a_report_written_by_the_attempt_replaces_a_stale_one(tmp_path):
    record = run_real(tmp_path, FAIL, [(ok(0), PASS)])
    assert (record.outcome, record.attempts) == (Outcome.PASSED, 1)


def test_the_retry_ignores_the_report_its_first_attempt_left(tmp_path):
    record = run_real(tmp_path, None, [(ok(137), FAIL), (ok(1), None)])
    assert (record.outcome, record.attempts) == (Outcome.INFRASTRUCTURE, 2)


def test_a_timeout_then_a_pass_ends_as_passed():
    timed_out = ProcResult(exit_code=-1, stdout=b"", stderr=b"", timed_out=True)
    runner = FakeRunner([timed_out, ok()])
    record = go(runner, reports(None, PASS))
    assert (record.outcome, record.attempts) == (Outcome.PASSED, 2)
    assert len(runner.stopped) == 1


def test_an_infrastructure_result_then_a_failure_ends_as_failed():
    runner = FakeRunner([ok(137), ok(1)])
    record = go(runner, reports(None, FAIL))
    assert (record.outcome, record.attempts) == (Outcome.FAILED, 2)
