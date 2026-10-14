import pytest

from rootflake.sandbox.results import (
    Outcome,
    TestId,
    classify,
    maven_test_command,
    parse_report,
)

PASS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="demo.DemoTest" tests="2" failures="0" errors="0" skipped="0">
  <testcase name="adds" classname="demo.DemoTest" time="0.01"/>
  <testcase name="other" classname="demo.DemoTest" time="0.01"/>
</testsuite>"""

FAIL_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<testsuite name="demo.DemoTest" tests="1" failures="1" errors="0" skipped="0">
  <testcase name="adds" classname="demo.DemoTest" time="0.01">
    <failure message="expected 2" type="java.lang.AssertionError">trace</failure>
  </testcase>
</testsuite>"""

ERROR_XML = PASS_XML.replace(
    b'<testcase name="adds" classname="demo.DemoTest" time="0.01"/>',
    b'<testcase name="adds" classname="demo.DemoTest"><error type="X">boom</error></testcase>',
)
SKIP_XML = PASS_XML.replace(
    b'<testcase name="adds" classname="demo.DemoTest" time="0.01"/>',
    b'<testcase name="adds" classname="demo.DemoTest"><skipped/></testcase>',
)

TEST = TestId.parse("demo.DemoTest#adds")


def test_test_id_parses_class_and_method():
    assert (TEST.class_name, TEST.method) == ("demo.DemoTest", "adds")


@pytest.mark.parametrize(
    "name",
    [
        "demo.DemoTest",
        "demo.DemoTest#",
        "#adds",
        "demo.DemoTest#adds -Dx=y",
        "demo.DemoTest#adds;rm",
        "demo..DemoTest#adds",
        "demo.DemoTest#adds\n",
        "demo.DemoTest#a+b",
        "demo.DemoTest#a*",
    ],
)
def test_test_id_rejects_anything_but_a_plain_name(name):
    with pytest.raises(ValueError):
        TestId.parse(name)


def test_test_id_accepts_nested_and_dollar_names():
    assert TestId.parse("a.b.Outer$Inner#test_1").class_name == "a.b.Outer$Inner"


def test_report_pass():
    assert parse_report(PASS_XML, TEST) == "passed"


def test_report_failure_and_error():
    assert parse_report(FAIL_XML, TEST) == "failed"
    assert parse_report(ERROR_XML, TEST) == "failed"


def test_report_skipped():
    assert parse_report(SKIP_XML, TEST) == "skipped"


def test_report_missing_testcase():
    assert parse_report(PASS_XML, TestId.parse("demo.DemoTest#absent")) == "missing"


def test_report_with_two_entries_for_the_test_is_ambiguous():
    doubled = PASS_XML.replace(b'name="other"', b'name="adds"')
    assert parse_report(doubled, TEST) == "ambiguous"


def test_report_with_a_doctype_is_refused():
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]>' + PASS_XML.split(b"?>", 1)[1]
    assert parse_report(evil, TEST) == "unreadable"


def test_oversized_report_is_refused():
    assert parse_report(PASS_XML + b" " * (9 * 1024 * 1024), TEST) == "unreadable"


def test_malformed_report_is_unreadable():
    assert parse_report(b"<testsuite><testcase", TEST) == "unreadable"


@pytest.mark.parametrize(
    ("exit_code", "report", "timed_out", "expected"),
    [
        (0, "passed", False, Outcome.PASSED),
        (1, "failed", False, Outcome.FAILED),
        (0, "failed", False, Outcome.INFRASTRUCTURE),
        (1, "passed", False, Outcome.INFRASTRUCTURE),
        (0, "missing", False, Outcome.INFRASTRUCTURE),
        (0, "skipped", False, Outcome.INFRASTRUCTURE),
        (0, "ambiguous", False, Outcome.INFRASTRUCTURE),
        (1, "unreadable", False, Outcome.INFRASTRUCTURE),
        (137, "passed", False, Outcome.INFRASTRUCTURE),
        (0, "passed", True, Outcome.TIMEOUT),
        (1, "missing", True, Outcome.TIMEOUT),
    ],
)
def test_pass_needs_exit_status_and_report_to_agree(exit_code, report, timed_out, expected):
    assert classify(exit_code, report, timed_out) is expected


def test_maven_command_runs_one_test_in_one_module_without_rebuilding_others():
    cmd = maven_test_command(TEST, module="core")
    assert cmd[:2] == ["mvn", "surefire:test"]
    assert "-Dtest=demo.DemoTest#adds" in cmd
    assert "-pl" in cmd and cmd[cmd.index("-pl") + 1] == "core"
    assert "-Dsurefire.failIfNoSpecifiedTests=false" not in cmd


def test_maven_command_for_the_root_module_has_no_pl():
    assert "-pl" not in maven_test_command(TEST, module="")


@pytest.mark.parametrize("module", ["../x", "/abs", "a b", "a,b", "-am", "core\n"])
def test_maven_command_rejects_unsafe_module_paths(module):
    with pytest.raises(ValueError):
        maven_test_command(TEST, module=module)


DTD_ATTACKS = {
    "internal_entity": b'<?xml version="1.0"?><!DOCTYPE t [<!ENTITY a "x">]>'
    b'<testsuite><testcase classname="demo.DemoTest" name="adds"/></testsuite>',
    "billion_laughs": b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa">'
    b'<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;"><!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;">]>'
    b'<testsuite><testcase classname="demo.DemoTest" name="adds">&c;</testcase></testsuite>',
    "external_entity": b'<?xml version="1.0"?><!DOCTYPE t [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
    b'<testsuite><testcase classname="demo.DemoTest" name="&e;"/></testsuite>',
}


@pytest.mark.parametrize("name", sorted(DTD_ATTACKS))
def test_any_dtd_makes_the_report_unreadable(name):
    assert parse_report(DTD_ATTACKS[name], TEST) == "unreadable"


def test_a_dtd_hidden_by_utf16_is_still_refused():
    data = (
        DTD_ATTACKS["internal_entity"]
        .decode()
        .replace('version="1.0"', 'version="1.0" encoding="UTF-16"')
    )
    assert parse_report(data.encode("utf-16"), TEST) == "unreadable"


def test_a_dtd_after_a_utf8_bom_is_still_refused():
    assert parse_report(b"\xef\xbb\xbf" + DTD_ATTACKS["internal_entity"], TEST) == "unreadable"


def test_a_utf8_bom_on_a_clean_report_is_fine():
    assert parse_report(b"\xef\xbb\xbf" + PASS_XML, TEST) == "passed"


def test_failure_nested_in_another_testcase_does_not_leak():
    other_failing = PASS_XML.replace(
        b'<testcase name="other" classname="demo.DemoTest" time="0.01"/>',
        b'<testcase name="other" classname="demo.DemoTest"><failure/></testcase>',
    )
    assert parse_report(other_failing, TEST) == "passed"


def _report(children):
    return (
        b'<testsuite><testcase classname="demo.DemoTest" name="adds">'
        + children
        + b"</testcase></testsuite>"
    )


@pytest.mark.parametrize("element", [b"flakyFailure", b"flakyError"])
def test_a_pass_after_a_surefire_rerun_is_flaky_not_passed(element):
    assert parse_report(_report(b"<" + element + b"/>"), TEST) == "flaky"


@pytest.mark.parametrize("element", [b"rerunFailure", b"rerunError"])
def test_a_test_that_failed_every_rerun_is_failed(element):
    assert parse_report(_report(b"<failure/><" + element + b"/>"), TEST) == "failed"
    assert parse_report(_report(b"<" + element + b"/>"), TEST) == "failed"


@pytest.mark.parametrize("exit_code", [0, 1])
def test_a_flaky_report_is_a_failed_run_whatever_the_exit_status(exit_code):
    assert classify(exit_code, "flaky", False) is Outcome.FAILED


def test_a_flaky_report_with_another_exit_status_is_infrastructure():
    assert classify(137, "flaky", False) is Outcome.INFRASTRUCTURE
