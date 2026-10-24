import json
import signal
import sys
from pathlib import Path

import pytest

from rootflake.gate.checker.check import Finding, Target, check
from rootflake.gate.isolated import WALL_LIMIT_S, check_isolated
from rootflake.sandbox.executor import ProcResult

JAVA = "src/test/java/org/example/FooTest.java"
FOO = "package org.example;\nclass FooTest {\n    void t() {}\n}\n"
TARGET = Target("org.example.FooTest", "t")
OK = ProcResult(0, b"", b"", False)


@pytest.fixture
def trees(tmp_path):
    pristine, workspace = tmp_path / "p", tmp_path / "w"
    (pristine / JAVA).parent.mkdir(parents=True)
    (pristine / JAVA).write_text(FOO)
    workspace.mkdir()
    return pristine, workspace


class Scripted:
    def __init__(self, proc, result=None):
        self.proc, self.result, self.calls = proc, result, []

    def run(self, args, timeout_s):
        self.calls.append((args, timeout_s))
        if self.result is not None:
            (Path(args[-1]) / "result.json").write_bytes(self.result)
        return self.proc

    def stop(self, name):
        raise AssertionError("no container")


def findings(items) -> bytes:
    return json.dumps({"findings": items}).encode()


def test_the_child_returns_what_the_checker_returns_in_process(trees):
    pristine, workspace = trees
    expected = check([JAVA], pristine, workspace, TARGET)
    assert expected.findings == (Finding("disable/test_file_removed", JAVA),)
    assert check_isolated([JAVA], pristine, workspace, TARGET) == expected


def test_the_request_names_the_check_step_and_its_inputs(trees):
    pristine, workspace = trees
    seen = {}

    class Peek(Scripted):
        def run(self, args, timeout_s):
            seen["args"] = args
            seen["request"] = json.loads((Path(args[-1]) / "request.json").read_text())
            return super().run(args, timeout_s)

    check_isolated([JAVA], pristine, workspace, TARGET, ["x"], runner=Peek(OK, findings([])))
    assert seen["args"][:5] == [sys.executable, "-I", "-m", "rootflake.gate.isolated", "check"]
    assert seen["request"] == {
        "paths": [JAVA],
        "pristine": str(pristine),
        "workspace": str(workspace),
        "target": {"class": "org.example.FooTest", "method": "t"},
        "overridden": ["x"],
    }


def test_the_wall_limit_applies(trees):
    runner = Scripted(OK, findings([]))
    check_isolated([JAVA], *trees, TARGET, runner=runner)
    assert runner.calls[0][1] == WALL_LIMIT_S


@pytest.mark.parametrize(
    "proc",
    [ProcResult(-1, b"", b"", True), ProcResult(-signal.SIGXCPU, b"", b"", False)],
    ids=["wall", "cpu"],
)
def test_a_limit_hit_is_a_checker_timeout(trees, proc):
    result = check_isolated([JAVA], *trees, TARGET, runner=Scripted(proc))
    assert result.findings == (Finding("checker/checker_timeout", None),)


@pytest.mark.parametrize(
    ("proc", "result"),
    [
        (ProcResult(1, b"", b"", False), None),
        (OK, None),
        (OK, b"{"),
        (OK, json.dumps({"findings": [], "x": 1}).encode()),
        (OK, findings([["unknown/code", JAVA, 1]])),
        (OK, findings([["disable/test_removed", "src/test/java/Other.java", 1]])),
        (OK, findings([["disable/test_removed", JAVA, "1"]])),
        (OK, findings([["disable/test_removed", JAVA]])),
        (OK, b"[" * 100000),
    ],
    ids=["exit_1", "no_result", "not_json", "extra_key", "unknown_type", "foreign_path",
         "line_not_int", "short_item", "deeply_nested"],
)  # fmt: skip
def test_a_failed_or_malformed_child_is_a_checker_error(trees, proc, result):
    outcome = check_isolated([JAVA], *trees, TARGET, runner=Scripted(proc, result))
    assert outcome.findings == (Finding("checker/checker_error", None),)


def test_a_finding_without_a_path_is_accepted_from_the_child(trees):
    runner = Scripted(OK, findings([["checker/unparsable", None, None]]))
    result = check_isolated([JAVA], *trees, TARGET, runner=runner)
    assert result.findings == (Finding("checker/unparsable", None, None),)
