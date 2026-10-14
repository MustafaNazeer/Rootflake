import json
import logging
import subprocess
from pathlib import Path

import pytest

from rootflake.corpus.github import (
    API_URL,
    PullInfo,
    fetch_pr,
    parse_pr_url,
    token_from_gh,
)
from rootflake.corpus.walk import TransientError, Unavailable

RECORDED = Path(__file__).parent.parent / "fixtures" / "github" / "apache-struts-pull-263.json"
TOKEN = "gho_" + "S" * 36


class Opener:
    def __init__(self, status, body=b""):
        self.status = status
        self.body = body
        self.requests = []

    def __call__(self, url, headers, timeout):
        self.requests.append((url, headers, timeout))
        if isinstance(self.status, Exception):
            raise self.status
        return self.status, self.body


def recorded(**change):
    data = json.loads(RECORDED.read_bytes())
    data.update(change)
    return json.dumps(data).encode()


@pytest.mark.parametrize(
    ("project", "pr", "expected"),
    [
        ("https://github.com/Apache/Struts", "https://github.com/apache/struts/pull/263",
         ("apache", "struts", 263)),
        ("https://github.com/o/r.js", "https://github.com/o/r.js/pull/1", ("o", "r.js", 1)),
    ],
)  # fmt: skip
def test_pr_urls_are_parsed_from_the_link(project, pr, expected):
    assert parse_pr_url(project, pr) == expected


@pytest.mark.parametrize(
    "pr",
    [
        "https://github.com/other/r/pull/1",
        "https://github.com/o/r/pull/0",
        "https://github.com/o/r/pull/1/files",
        "https://github.com/o/r/issues/1",
        "http://github.com/o/r/pull/1",
        "https://github.com.evil/o/r/pull/1",
        "https://github.com/o/../pull/1",
    ],
)
def test_other_links_are_refused(pr):
    with pytest.raises(ValueError):
        parse_pr_url("https://github.com/o/r", pr)


def test_a_merged_pr_gives_its_commits_and_merge_time():
    opener = Opener(200, RECORDED.read_bytes())
    info = fetch_pr("apache", "struts", 263, TOKEN, opener)
    assert info == PullInfo(
        merged_at="2018-11-18T11:48:50Z",
        merge_commit_sha="299d91e52bb39112921079b808bbeefee3a4c0f4",
        head_sha="2d4e26ff8ab727b625aa744235523729cbb74c5b",
        base_sha="af3ee0747410ffb64448e933888ff4a15d72fe10",
    )
    url, headers, _ = opener.requests[0]
    assert url == f"{API_URL}/repos/apache/struts/pulls/263"
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in url


def test_an_unmerged_pr_is_unavailable():
    # synthetic: the recorded response with merged set to false
    result = fetch_pr("o", "r", 1, TOKEN, Opener(200, recorded(merged=False, merged_at=None)))
    assert isinstance(result, Unavailable)


def test_a_missing_pr_is_unavailable():
    assert isinstance(fetch_pr("o", "r", 1, TOKEN, Opener(404, b"{}")), Unavailable)


@pytest.mark.parametrize("status", [401, 403, 429, 500, 502, 503, 302])
def test_other_statuses_are_transient(status):
    with pytest.raises(TransientError):
        fetch_pr("o", "r", 1, TOKEN, Opener(status, b"{}"))


@pytest.mark.parametrize("error", [OSError("connection reset"), TimeoutError("timed out")])
def test_network_errors_are_transient(error):
    with pytest.raises(TransientError):
        fetch_pr("o", "r", 1, TOKEN, Opener(error))


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        recorded(merge_commit_sha="xyz"),
        recorded(merged_at="yesterday"),
        recorded(head={"sha": None}),
        json.dumps([1]).encode(),
    ],
)
def test_a_malformed_response_is_transient_not_a_rule_outcome(body):
    with pytest.raises(TransientError):
        fetch_pr("o", "r", 1, TOKEN, Opener(200, body))


def test_the_token_never_appears_in_errors_or_logs(caplog):
    caplog.set_level(logging.DEBUG)
    leaky = OSError(f"failed with Authorization: Bearer {TOKEN}")
    for opener in (Opener(403, TOKEN.encode()), Opener(leaky), Opener(200, TOKEN.encode())):
        with pytest.raises(TransientError) as caught:
            fetch_pr("o", "r", 1, TOKEN, opener)
        assert TOKEN not in str(caught.value)
        error = caught.value
        assert error.__cause__ is None
        assert error.__context__ is None or error.__suppress_context__
    assert TOKEN not in caplog.text


def test_the_token_comes_from_gh_auth_token():
    def run(args, **kwargs):
        assert args == ["gh", "auth", "token"]
        return subprocess.CompletedProcess(args, 0, stdout=TOKEN + "\n", stderr="")

    assert token_from_gh(run) == TOKEN


@pytest.mark.parametrize("returncode", [0, 1])
def test_no_token_is_an_error_that_does_not_echo_output(returncode):
    def run(args, **kwargs):
        return subprocess.CompletedProcess(args, returncode, stdout="", stderr="secret detail")

    with pytest.raises(RuntimeError) as caught:
        token_from_gh(run)
    assert "secret detail" not in str(caught.value)
