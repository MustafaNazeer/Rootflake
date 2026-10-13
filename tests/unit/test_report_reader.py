import os
from pathlib import Path

import pytest

from rootflake.sandbox.reports import read_report

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
