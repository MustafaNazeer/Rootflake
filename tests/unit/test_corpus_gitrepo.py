import os
import subprocess

import pytest

from rootflake.corpus import gitrepo
from rootflake.corpus.gitrepo import GitError, GitRepo, git_config, git_env

PLAIN_ENV = {
    "PATH": os.environ["PATH"],
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}


def git(cwd, *args):
    result = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, env=PLAIN_ENV, capture_output=True, check=True,
    )  # fmt: skip
    return result.stdout.decode().strip()


@pytest.fixture
def source(tmp_path):
    """A repository with a submodule, an outside symlink, a diff driver, and a PR ref."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    git(sub, "init", "-q")
    (sub / "s.txt").write_text("sub\n")
    git(sub, "add", ".")
    git(sub, "commit", "-qm", "sub")

    repo = tmp_path / "src"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "pom.xml").write_text("<project/>\n")
    (repo / ".gitattributes").write_text("* diff=evil\n")
    (repo / "A.java").write_text("class A {}\n")
    (repo / "link.xml").symlink_to(outside)
    git(repo, "submodule", "add", "-q", str(sub), "sub")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-qb", "fix")
    (repo / "A.java").write_text("class A { int x; }\n")
    git(repo, "commit", "-qam", "fix")
    head = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/pull/1/head", head)
    git(repo, "checkout", "-q", "main")
    (repo / "B.java").write_text("class B {}\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "main moves on")
    git(repo, "branch", "-D", "fix", "-q")
    return {"path": repo, "base": base, "head": head, "main": git(repo, "rev-parse", "HEAD")}


@pytest.fixture
def mirror(source, tmp_path):
    dest = tmp_path / "mirror"
    dest.mkdir()
    return GitRepo.clone(str(source["path"]), dest, protocols=("file",))


class Spy:
    def __init__(self):
        self.calls = []

    def __call__(self, args, env, timeout):
        self.calls.append((args, env, timeout))
        return subprocess.CompletedProcess(args, 0, b"", b"")


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/x",
        "/tmp/x",
        "https://gitlab.com/o/r",
        "https://github.com/o/r/pull/1",
        "ssh://git@github.com/o/r",
        "https://github.com/o/..",
        "--upload-pack=touch /tmp/pwned",
    ],
)
def test_mirror_refuses_anything_but_a_github_project_before_git_runs(url, tmp_path):
    spy = Spy()
    with pytest.raises(ValueError):
        GitRepo.mirror(url, tmp_path, run=spy)
    assert spy.calls == []


def test_mirror_runs_one_hardened_bare_clone(tmp_path):
    spy = Spy()
    GitRepo.mirror("https://github.com/Apache/Struts", tmp_path, run=spy)
    ((args, env, _),) = spy.calls
    assert args == [
        "git", *git_config(("https",)), "clone", "--bare", "--no-recurse-submodules", "--quiet",
        "--", "https://github.com/Apache/Struts", str(tmp_path),
    ]  # fmt: skip
    assert env == git_env()


def test_the_config_and_environment_close_every_known_door():
    config = git_config(("https",))
    pairs = {config[i + 1] for i in range(0, len(config), 2)}
    assert {
        "protocol.allow=never",
        "protocol.https.allow=always",
        "protocol.file.allow=never",
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "submodule.recurse=false",
        "fetch.recurseSubmodules=false",
        "gc.auto=0",
        "maintenance.auto=false",
    } <= pairs
    env = git_env()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert not {key for key in env if key.startswith("GIT_") and key not in {
        "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT"}}  # fmt: skip


def test_a_local_path_is_refused_by_git_itself_under_the_default_protocols(source, tmp_path):
    dest = tmp_path / "m"
    dest.mkdir()
    with pytest.raises(GitError):
        GitRepo.clone(str(source["path"]), dest, protocols=("https",))


def test_the_mirror_is_bare_and_fetches_no_submodule(mirror):
    files = {p.name for p in mirror.path.iterdir()}
    assert "HEAD" in files and "objects" in files
    assert not {"pom.xml", "A.java", "sub", "modules", "link.xml"} & files


def test_the_pr_head_is_fetched_by_its_pull_ref(mirror, source):
    assert mirror.fetch_pr_head(1) == source["head"]
    assert mirror.fetch_pr_head(2) is None


def test_a_hook_in_the_mirror_never_runs(mirror, tmp_path):
    marker = tmp_path / "hook-ran"
    hook = mirror.path / "hooks" / "reference-transaction"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    mirror.fetch_pr_head(1)
    assert not marker.exists()


def test_the_canonical_diff_is_head_against_its_merge_base(mirror, source, monkeypatch, tmp_path):
    marker = tmp_path / "driver-ran"
    hostile = tmp_path / "hostile.gitconfig"
    driver = f"sh -c 'touch {marker}' sh"
    hostile.write_text(f'[diff "evil"]\n\tcommand = {driver}\n\ttextconv = {driver}\n')
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(hostile))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", f"'diff.evil.command=touch {marker}'")
    head = mirror.fetch_pr_head(1)
    diff = mirror.canonical_diff(head, source["main"])
    expected = subprocess.run(
        ["git", "diff", "--no-color", "--no-ext-diff", "--no-textconv", source["base"], head],
        cwd=source["path"], env=PLAIN_ENV, capture_output=True, check=True,
    ).stdout  # fmt: skip
    assert diff == expected
    assert b"int x" in diff
    assert not marker.exists()


def test_a_missing_commit_is_none_for_the_diff(mirror, source):
    assert mirror.canonical_diff("f" * 40, source["main"]) is None


def test_read_blob_returns_regular_files_at_a_commit(mirror, source):
    assert mirror.read_blob(source["base"], "pom.xml") == b"<project/>\n"


@pytest.mark.parametrize("path", ["link.xml", "sub", "missing.xml", "sub/s.txt"])
def test_read_blob_never_follows_links_or_gitlinks(mirror, source, path):
    assert mirror.read_blob(source["base"], path) is None


def test_read_blob_refuses_an_oversized_file(mirror, source):
    assert mirror.read_blob(source["base"], "pom.xml", limit=5) is None


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "a//b", "-x", "", "a/./b"])
def test_read_blob_paths_must_be_plain_relative_paths(mirror, source, path):
    with pytest.raises(ValueError):
        mirror.read_blob(source["base"], path)


def test_read_blob_at_an_unknown_commit_is_none(mirror):
    assert mirror.read_blob("f" * 40, "pom.xml") is None


@pytest.mark.parametrize("sha", ["HEAD", "main", "f" * 39, "--all"])
def test_commits_must_be_full_shas(mirror, sha):
    with pytest.raises(ValueError):
        mirror.read_blob(sha, "pom.xml")


def test_ensure_commit(mirror, source):
    assert mirror.ensure_commit(source["main"])
    assert not mirror.ensure_commit("f" * 40)


def test_git_version_is_recorded():
    assert gitrepo.git_version().startswith("git version ")


def test_a_hung_git_is_a_git_error(tmp_path):
    def hung(args, env, timeout):
        raise subprocess.TimeoutExpired(args, timeout)

    with pytest.raises(GitError, match="timed out"):
        GitRepo.mirror("https://github.com/o/r", tmp_path, run=hung)


def test_control_the_planted_hook_runs_under_plain_git(mirror, source, tmp_path):
    marker = tmp_path / "hook-ran"
    hook = mirror.path / "hooks" / "reference-transaction"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    git(mirror.path, "fetch", "-q", "origin", "+refs/pull/1/head:refs/control")
    assert marker.exists()


def test_control_the_planted_diff_driver_runs_under_plain_git(source, tmp_path):
    marker = tmp_path / "driver-ran"
    hostile = tmp_path / "hostile.gitconfig"
    hostile.write_text(f"[diff \"evil\"]\n\tcommand = sh -c 'touch {marker}' sh\n")
    subprocess.run(
        ["git", "diff", source["base"], source["main"]],
        cwd=source["path"], env={**PLAIN_ENV, "GIT_CONFIG_GLOBAL": str(hostile)},
        capture_output=True, check=True,
    )  # fmt: skip
    assert marker.exists()


class Scripted:
    """Answers git calls by subcommand: (returncode, stdout, stderr) or an exception."""

    def __init__(self, answers):
        self.answers = answers

    def __call__(self, args, env, timeout):
        sub = args[args.index("--git-dir") + 2]
        answer = self.answers.get(sub, (0, b"", b""))
        if isinstance(answer, BaseException):
            raise answer
        code, out, err = answer
        return subprocess.CompletedProcess(args, code, out, err)


def scripted(tmp_path, **answers):
    return GitRepo(tmp_path / "m", ("https",), Scripted(answers))


C = "c" * 40
B = "b" * 40


def test_a_missing_object_is_only_the_not_a_valid_object_answer(tmp_path):
    repo = scripted(tmp_path, **{"cat-file": (128, b"", b"fatal: Not a valid object name x")})
    assert repo.read_blob(C, "pom.xml") is None


@pytest.mark.parametrize(
    "answer",
    [
        (128, b"", b"fatal: not a git repository: '/m'"),
        (128, b"", b"fatal: unable to read 1234: corrupt loose object"),
        subprocess.TimeoutExpired(["git"], 300),
    ],
)
def test_other_object_lookup_failures_raise(tmp_path, answer):
    repo = scripted(tmp_path, **{"cat-file": answer})
    with pytest.raises(GitError):
        repo.read_blob(C, "pom.xml")
    with pytest.raises(GitError):
        repo.ensure_commit(C)


def test_no_merge_base_is_none(tmp_path):
    repo = scripted(tmp_path, **{"merge-base": (1, b"", b"")})
    assert repo.canonical_diff(C, B) is None


@pytest.mark.parametrize(
    "answer",
    [(128, b"", b"fatal: Not a valid commit name"), subprocess.TimeoutExpired(["git"], 300)],
)
def test_other_merge_base_failures_raise(tmp_path, answer):
    repo = scripted(tmp_path, **{"merge-base": answer})
    with pytest.raises(GitError):
        repo.canonical_diff(C, B)


def test_a_timeout_kills_the_whole_process_group(tmp_path):
    pid_file = tmp_path / "child"
    script = f"sleep 30 & echo $! > {pid_file}; wait"
    with pytest.raises(subprocess.TimeoutExpired):
        gitrepo._run(["sh", "-c", script], {"PATH": os.environ["PATH"]}, 0.5)
    child = int(pid_file.read_text())
    for _ in range(50):
        if not os.path.exists(f"/proc/{child}") or "Z" in _state(child):
            break
        import time

        time.sleep(0.05)
    assert not os.path.exists(f"/proc/{child}") or "Z" in _state(child)


def _state(pid):
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return "gone"
