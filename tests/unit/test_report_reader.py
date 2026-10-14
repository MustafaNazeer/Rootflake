import os
import time
from pathlib import Path

import pytest

from rootflake.sandbox.reports import read_report, report_stamp

REPORT = Path("core/target/surefire-reports/TEST-demo.DemoTest.xml")


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / REPORT.parent).mkdir(parents=True)
    return ws


def test_reads_a_regular_report(workspace):
    (workspace / REPORT).write_bytes(b"<testsuite/>")
    assert read_report(workspace, REPORT) == b"<testsuite/>"


def test_missing_report_is_none(workspace):
    assert read_report(workspace, REPORT) is None


def test_symlinked_report_file_is_refused(workspace, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"<x/>")
    (workspace / REPORT).symlink_to(secret)
    assert read_report(workspace, REPORT) is None


def test_symlinked_directory_is_refused(workspace, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / REPORT.name).write_bytes(b"<x/>")
    reports = workspace / REPORT.parent
    reports.rmdir()
    reports.symlink_to(outside)
    assert read_report(workspace, REPORT) is None


def test_fifo_is_refused_without_blocking(workspace):
    os.mkfifo(workspace / REPORT)
    assert read_report(workspace, REPORT) is None


def test_oversized_report_is_truncated_to_the_cap_plus_one(workspace):
    (workspace / REPORT).write_bytes(b"x" * 100)
    assert len(read_report(workspace, REPORT, limit=10)) == 11


@pytest.mark.parametrize("relative", [Path("/etc/passwd"), Path("../x.xml"), Path("a/../../x.xml")])
def test_paths_must_stay_inside_the_workspace(workspace, relative):
    with pytest.raises(ValueError):
        read_report(workspace, relative)


def test_a_missing_report_has_no_stamp(workspace):
    assert report_stamp(workspace, REPORT) is None


def test_a_symlinked_report_has_no_stamp(workspace, tmp_path):
    secret = tmp_path / "secret"
    secret.write_bytes(b"<x/>")
    (workspace / REPORT).symlink_to(secret)
    assert report_stamp(workspace, REPORT) is None


def test_an_unchanged_report_reads_as_missing(workspace):
    (workspace / REPORT).write_bytes(b"<testsuite/>")
    stale = report_stamp(workspace, REPORT)
    assert read_report(workspace, REPORT, stale=stale) is None


def test_a_replaced_report_is_read(workspace):
    (workspace / REPORT).write_bytes(b"<old/>")
    stale = report_stamp(workspace, REPORT)
    (workspace / REPORT).unlink()
    (workspace / REPORT).write_bytes(b"<new/>")
    assert read_report(workspace, REPORT, stale=stale) == b"<new/>"


def test_a_report_rewritten_in_place_is_read(workspace):
    (workspace / REPORT).write_bytes(b"<same/>")
    stale = report_stamp(workspace, REPORT)
    deadline = time.monotonic() + 3
    while report_stamp(workspace, REPORT) == stale and time.monotonic() < deadline:
        time.sleep(0.01)
        with open(workspace / REPORT, "r+b") as handle:
            handle.write(b"<same/>")
    assert read_report(workspace, REPORT, stale=stale) == b"<same/>"
