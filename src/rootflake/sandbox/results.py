"""Turns one sandboxed test execution into an outcome.

A run counts as passed only when the process exit status and the parsed Surefire report
agree (SH-17). Reports are written by code under test, so they are size capped and parsed
with expat handlers that refuse any DOCTYPE or entity declaration inside the parser itself,
whatever the document's encoding.
"""

import re
from dataclasses import dataclass
from enum import Enum
from xml.parsers import expat

from rootflake.sandbox.xmlsafe import RefusedXml, new_parser

MAX_REPORT_BYTES = 8 * 1024 * 1024

_IDENT = r"[A-Za-z_$][A-Za-z0-9_$]*"
TEST_ID_RE = re.compile(rf"({_IDENT}(?:\.{_IDENT})*)#({_IDENT})")
MODULE_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*(/[A-Za-z0-9_][A-Za-z0-9_.-]*)*")


@dataclass(frozen=True)
class TestId:
    __test__ = False

    class_name: str
    method: str

    @classmethod
    def parse(cls, name: str) -> TestId:
        match = TEST_ID_RE.fullmatch(name)
        if not match:
            raise ValueError(f"not a plain Class#method test name: {name!r}")
        return cls(match.group(1), match.group(2))

    def __str__(self) -> str:
        return f"{self.class_name}#{self.method}"


class Outcome(Enum):
    PASSED = "passed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    INFRASTRUCTURE = "infrastructure"


def parse_report(data: bytes, test: TestId) -> str:
    """Return passed, failed, flaky, skipped, missing, ambiguous, or unreadable."""
    if len(data) > MAX_REPORT_BYTES:
        return "unreadable"
    matches: list[set[str]] = []
    depth_of_match: list[int] = []
    depth = 0

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth
        depth += 1
        if (
            name == "testcase"
            and attrs.get("classname") == test.class_name
            and attrs.get("name") == test.method
        ):
            matches.append(set())
            depth_of_match.append(depth)
        elif depth_of_match and depth == depth_of_match[-1] + 1:
            matches[-1].add(name)

    def end(_name: str) -> None:
        nonlocal depth
        if depth_of_match and depth == depth_of_match[-1]:
            depth_of_match.pop()
        depth -= 1

    parser = new_parser()
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    try:
        parser.Parse(data, True)
    except RefusedXml, expat.ExpatError:
        return "unreadable"
    if not matches:
        return "missing"
    if len(matches) > 1:
        return "ambiguous"
    children = matches[0]
    if children & {"failure", "error", "rerunFailure", "rerunError"}:
        return "failed"
    if children & {"flakyFailure", "flakyError"}:
        return "flaky"
    if "skipped" in children:
        return "skipped"
    return "passed"


def classify(exit_code: int, report: str, timed_out: bool) -> Outcome:
    if timed_out:
        return Outcome.TIMEOUT
    if exit_code == 0 and report == "passed":
        return Outcome.PASSED
    if exit_code == 1 and report == "failed":
        return Outcome.FAILED
    if exit_code in (0, 1) and report == "flaky":
        # Surefire passes a test that failed and then passed a rerun; one failure is a failure.
        return Outcome.FAILED
    return Outcome.INFRASTRUCTURE


def maven_test_command(test: TestId, module: str) -> list[str]:
    command = ["mvn", "surefire:test", f"-Dtest={test}"]
    if module:
        if not MODULE_RE.fullmatch(module) or ".." in module.split("/"):
            raise ValueError(f"unsafe module path: {module!r}")
        command += ["-pl", module]
    return command
