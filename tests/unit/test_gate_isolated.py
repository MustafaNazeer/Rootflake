import json
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from rootflake.gate import isolated
from rootflake.gate.isolated import (
    ADDRESS_SPACE_BYTES,
    CPU_LIMIT_S,
    RESULT_LIMIT_BYTES,
    WALL_LIMIT_S,
    validate_isolated,
)
from rootflake.gate.validator import Rejection, validate
from rootflake.sandbox.executor import DockerRunner, ProcResult

PROJECT = "https://github.com/example/demo"
TEST = "org.example.FooTest#testFoo"
TEST_FILE = "src/test/java/org/example/FooTest.java"
FOO = "class FooTest {\n    int a = 1;\n}\n"
PATCH = (
    f"diff --git a/{TEST_FILE} b/{TEST_FILE}\n"
    f"--- a/{TEST_FILE}\n+++ b/{TEST_FILE}\n"
    "@@ -1,3 +1,3 @@\n class FooTest {\n-    int a = 1;\n+    int a = 2;\n }\n"
).encode()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / TEST_FILE).parent.mkdir(parents=True)
    (root / "pom.xml").write_text("<project/>\n")
    (root / TEST_FILE).write_text(FOO)
    return root


class ScriptedRunner:
    """Stands in for the child: writes `result` into the request folder, then returns `proc`."""

    def __init__(self, proc: ProcResult, result: bytes | None = None):
        self.proc = proc
        self.result = result
        self.calls = []

    def run(self, args, timeout_s):
        self.calls.append((args, timeout_s))
        if self.result is not None:
            (Path(args[-1]) / "result.json").write_bytes(self.result)
        return self.proc

    def stop(self, name):
        raise AssertionError("no container to stop")


OK = ProcResult(0, b"", b"", False)


def test_the_child_returns_what_the_validator_returns_in_process(repo):
    assert validate_isolated(PATCH, repo, PROJECT, TEST) == validate(PATCH, repo, PROJECT, TEST)


def test_the_child_reports_a_rejection_with_its_path(repo):
    bad = PATCH.replace(b"FooTest.java", b"CON.java")
    result = validate_isolated(bad, repo, PROJECT, TEST)
    assert result.rejections == (Rejection("path_syntax", "src/test/java/org/example/CON.java"),)


def test_the_child_runs_python_in_isolated_mode_under_the_wall_limit(repo):
    runner = ScriptedRunner(
        OK, json.dumps({"rejections": [], "changes": [], "overridden": []}).encode()
    )
    validate_isolated(PATCH, repo, PROJECT, TEST, runner=runner)
    [(args, timeout_s)] = runner.calls
    assert args[:4] == [sys.executable, "-I", "-m", "rootflake.gate.isolated"]
    assert timeout_s == WALL_LIMIT_S


def test_the_request_carries_no_override_list(repo):
    seen = {}

    class Peek(ScriptedRunner):
        def run(self, args, timeout_s):
            folder = Path(args[-1])
            seen["request"] = json.loads((folder / "request.json").read_text())
            seen["files"] = sorted(p.name for p in folder.iterdir())
            return super().run(args, timeout_s)

    validate_isolated(PATCH, repo, PROJECT, TEST, runner=Peek(OK, b"{}"))
    assert sorted(seen["request"]) == ["pristine", "project", "test"]
    assert seen["files"] == ["patch.diff", "request.json"]


def test_the_request_folder_is_removed_afterwards(repo):
    runner = ScriptedRunner(OK, b"{}")
    validate_isolated(PATCH, repo, PROJECT, TEST, runner=runner)
    assert not Path(runner.calls[0][0][-1]).exists()


def test_a_wall_timeout_is_a_validator_timeout(repo):
    runner = ScriptedRunner(ProcResult(-1, b"", b"", True))
    result = validate_isolated(PATCH, repo, PROJECT, TEST, runner=runner)
    assert result.rejections == (Rejection("validator_timeout"),)


def test_a_cpu_limit_kill_is_a_validator_timeout(repo):
    runner = ScriptedRunner(ProcResult(-signal.SIGXCPU, b"", b"", False))
    result = validate_isolated(PATCH, repo, PROJECT, TEST, runner=runner)
    assert result.rejections == (Rejection("validator_timeout"),)


@pytest.mark.parametrize(
    ("proc", "result"),
    [
        (ProcResult(1, b"", b"Traceback", False), None),
        (ProcResult(-signal.SIGKILL, b"", b"", False), None),
        (OK, None),
        (OK, b"not json"),
        (OK, b"[]"),
        (OK, json.dumps({"rejections": [[1, None]], "changes": [], "overridden": []}).encode()),
        (
            OK,
            json.dumps(
                {"rejections": [], "changes": [["a", "yes", ""]], "overridden": []}
            ).encode(),
        ),
        (
            OK,
            json.dumps(
                {"rejections": [], "changes": [["a", False, "!!"]], "overridden": []}
            ).encode(),
        ),
        (OK, json.dumps({"rejections": [], "changes": [], "overridden": [], "x": 1}).encode()),
        (OK, b" " * (RESULT_LIMIT_BYTES + 1)),
        (OK, b"[" * 100000),
    ],
    ids=[
        "exit_1",
        "killed",
        "no_result",
        "not_json",
        "not_an_object",
        "code_not_a_string",
        "created_not_a_bool",
        "content_not_base64",
        "extra_key",
        "result_too_large",
        "deeply_nested",
    ],
)
def test_a_failed_or_malformed_child_is_a_validator_error(repo, proc, result):
    runner = ScriptedRunner(proc, result)
    assert validate_isolated(PATCH, repo, PROJECT, TEST, runner=runner).rejections == (
        Rejection("validator_error"),
    )


def test_a_rejection_and_a_change_together_are_a_validator_error(repo):
    result = {
        "rejections": [["format", None]],
        "changes": [[TEST_FILE, False, ""]],
        "overridden": [],
    }
    runner = ScriptedRunner(OK, json.dumps(result).encode())
    assert validate_isolated(PATCH, repo, PROJECT, TEST, runner=runner).rejections == (
        Rejection("validator_error"),
    )


def test_the_default_limits_are_thirty_seconds_one_gib_and_sixty_seconds():
    assert (CPU_LIMIT_S, ADDRESS_SPACE_BYTES, WALL_LIMIT_S) == (30, 1 << 30, 60)
    assert isinstance(isolated.DEFAULT_RUNNER, DockerRunner)


def child(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-c", code], capture_output=True, timeout=60, check=False
    )


def test_the_child_limits_are_set_as_hard_limits():
    done = child(
        "import resource\n"
        "from rootflake.gate.isolated import apply_limits\n"
        "apply_limits()\n"
        "print(resource.getrlimit(resource.RLIMIT_CPU), resource.getrlimit(resource.RLIMIT_AS),"
        " resource.getrlimit(resource.RLIMIT_CORE))\n"
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.decode().split() == [
        f"({CPU_LIMIT_S},",
        f"{CPU_LIMIT_S})",
        f"({ADDRESS_SPACE_BYTES},",
        f"{ADDRESS_SPACE_BYTES})",
        "(0,",
        "0)",
    ]


def test_the_address_space_limit_stops_a_large_allocation():
    done = child(
        "from rootflake.gate.isolated import apply_limits\n"
        "apply_limits()\n"
        "data = bytearray(2 << 30)\n"
    )
    assert done.returncode != 0
    assert b"MemoryError" in done.stderr


def test_the_cpu_limit_stops_a_busy_loop():
    done = child(
        "from rootflake.gate.isolated import apply_limits\n"
        "apply_limits(cpu_s=1)\n"
        "while True:\n    pass\n"
    )
    assert done.returncode in (-signal.SIGXCPU, -signal.SIGKILL)
