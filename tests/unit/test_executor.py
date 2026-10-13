import tempfile
from pathlib import Path

import pytest

from rootflake.sandbox.container import RunSpec, SandboxLimits
from rootflake.sandbox.executor import ProcResult, report_path, run_test, sanitize_output
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
    return lambda workspace, relative: queue.pop(0)


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


def test_output_is_capped_and_stripped_of_terminal_controls():
    text = sanitize_output(b"ok\x1b[31mred\x1b[0m\x07\r\n" + b"x" * 100, limit=20)
    assert "\x1b" not in text and "\x07" not in text
    assert text.startswith("okred")
    assert len(text) <= 20 + len("\n[truncated]")
    assert text.endswith("[truncated]")


def test_invalid_utf8_output_does_not_raise():
    assert sanitize_output(b"\xff\xfe ok", limit=100).endswith("ok")


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


def test_docker_runner_bounds_output_from_a_noisy_process():
    from rootflake.sandbox.executor import OUTPUT_LIMIT, DockerRunner

    noisy = ["python3", "-c", "import sys\nfor _ in range(4000): sys.stdout.write('x' * 4096)"]
    result = DockerRunner().run(noisy, timeout_s=60)
    assert result.exit_code == 0
    assert len(result.stdout) == OUTPUT_LIMIT + 1


def test_docker_runner_reports_a_timeout():
    from rootflake.sandbox.executor import DockerRunner

    result = DockerRunner().run(["sleep", "5"], timeout_s=0.5)
    assert result.timed_out
