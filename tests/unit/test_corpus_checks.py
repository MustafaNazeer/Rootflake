import hashlib
import json

import pytest

from rootflake.corpus import __main__ as cli
from rootflake.corpus.checks import CorpusChecks, module_poms
from rootflake.corpus.github import PullInfo
from rootflake.corpus.gitrepo import GitError
from rootflake.corpus.idoft import Row
from rootflake.corpus.walk import FixDiff, TransientError, Unavailable

SHA = "a" * 40
INFO = PullInfo("2020-01-01T00:00:00Z", "c" * 40, "b" * 40, "d" * 40)


def row(module=".", project="https://github.com/Apache/Struts", pr=263):
    return Row(1, project, SHA, module, "p.C.m", "ID", "Accepted", f"{project}/pull/{pr}")


class Repo:
    def __init__(self, head="b" * 40, diff=b"diff\n", blobs=None, has_commit=True):
        self.head = head
        self.diff = diff
        self.blobs = blobs or {}
        self.has_commit = has_commit

    def fetch_pr_head(self, number):
        return self.head

    def canonical_diff(self, head, base):
        assert (head, base) == (INFO.head_sha, INFO.base_sha)
        return self.diff

    def ensure_commit(self, sha):
        return self.has_commit

    def read_blob(self, sha, path):
        return self.blobs.get(path)


def checks(repo=None, info=INFO, mirror_error=None):
    calls = {"fetch": [], "mirror": []}

    def fetch(owner, name, number, token):
        calls["fetch"].append((owner, name, number, token))
        return info

    def mirror(project_url):
        calls["mirror"].append(project_url)
        if mirror_error:
            raise mirror_error
        return repo or Repo()

    return CorpusChecks("tok", fetch=fetch, mirror=mirror), calls


def test_a_merged_pr_with_a_fetchable_head_gives_the_diff_hash():
    corpus, calls = checks()
    assert corpus.fix_diff(row()) == FixDiff(
        "b" * 40, "c" * 40, "2020-01-01T00:00:00Z", hashlib.sha256(b"diff\n").hexdigest()
    )
    assert calls["fetch"] == [("apache", "struts", 263, "tok")]


def test_the_mirror_is_made_once_per_project():
    corpus, calls = checks()
    corpus.fix_diff(row(pr=1))
    corpus.fix_diff(row(pr=2))
    corpus.java_level(row())
    assert calls["mirror"] == ["https://github.com/Apache/Struts"]


@pytest.mark.parametrize(
    ("corpus", "detail"),
    [
        (checks(info=Unavailable("pull request not merged"))[0], "pull request not merged"),
        (checks(Repo(head=None))[0], "pull request head cannot be fetched"),
        (checks(Repo(head="e" * 40))[0], "pull ref head differs from the API head"),
        (checks(Repo(diff=None))[0], "canonical diff cannot be computed"),
        (
            checks(mirror_error=GitError("git clone failed: remote: Repository not found."))[0],
            "repository cannot be cloned",
        ),
    ],
)
def test_definitive_failures_are_rule_nine(corpus, detail):
    assert corpus.fix_diff(row()) == Unavailable(detail)


def test_any_other_git_failure_aborts_the_walk():
    corpus, _ = checks(mirror_error=GitError("git clone failed: Could not resolve host"))
    with pytest.raises(TransientError):
        corpus.fix_diff(row())


@pytest.mark.parametrize(
    ("module", "paths"),
    [
        (".", ["pom.xml"]),
        ("core", ["core/pom.xml", "pom.xml"]),
        ("a/b/", ["a/b/pom.xml", "a/pom.xml", "pom.xml"]),
    ],
)
def test_module_poms_run_from_the_module_to_the_root(module, paths):
    assert module_poms(module) == paths


@pytest.mark.parametrize("module", ["../x", "/abs", "a/../b", "-x", ""])
def test_unsafe_module_paths_have_no_poms(module):
    assert module_poms(module) is None


def test_the_java_level_reads_the_module_chain_at_the_detected_commit():
    blobs = {
        "core/pom.xml": b"<project><properties><x>1</x></properties></project>",
        "pom.xml": b"<project><properties>"
        b"<maven.compiler.release>17</maven.compiler.release></properties></project>",
    }
    corpus, _ = checks(Repo(blobs=blobs))
    assert corpus.java_level(row("core")) == 17


@pytest.mark.parametrize(
    "repo", [Repo(has_commit=False), Repo(blobs={}), Repo(blobs={"pom.xml": b"<x"})]
)
def test_an_unreadable_level_is_undeclared(repo):
    corpus, _ = checks(repo)
    assert corpus.java_level(row()) is None


@pytest.mark.parametrize("call", ["build", "runnable"])
def test_rules_eleven_and_twelve_wait_for_the_sandbox(call):
    corpus, _ = checks()
    with pytest.raises(NotImplementedError, match="sandbox"):
        getattr(corpus, call)(*([row()] if call == "build" else [row(), 11]))


def test_the_draw_refuses_to_run(capsys):
    assert cli.main(["draw"]) == 2
    assert "R" in capsys.readouterr().err


def test_screen_without_the_cached_file_says_how_to_get_it(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "default_root", lambda: tmp_path)
    assert cli.main(["screen"]) == 1
    assert "fetch-idoft" in capsys.readouterr().err


def test_screen_prints_the_counts_and_the_plan(tmp_path, monkeypatch, capsys):
    from rootflake.corpus import idoft

    header = ",".join(idoft.HEADER)
    lines = [header] + [
        f"https://github.com/o/r{n},{SHA},.,p.C{n}.m,ID,Accepted,https://github.com/o/r{n}/pull/{n},"
        for n in range(1, 8)
    ]
    data = ("\n".join(lines) + "\n").encode()
    path = idoft.cache_path(tmp_path, idoft.PINNED_COMMIT)
    path.parent.mkdir(parents=True)
    path.write_bytes(data)
    monkeypatch.setattr(cli, "default_root", lambda: tmp_path)
    monkeypatch.setattr(idoft, "PINNED_SHA256", hashlib.sha256(data).hexdigest())
    assert cli.main(["screen"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["eligible_prs"] == {"ID": 7}
    assert report["plan"] == {"ID": [7, 0]}
    assert report["funnel"] == {"status_not_accepted": 0}
