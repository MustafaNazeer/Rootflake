"""The host side bare clone of a target project, the one place the host handles repository content.

A bare clone has no working tree, so nothing from the target is ever checked out on the host:
files are read as blobs by commit and path, symlinks and gitlinks are never followed, and
submodules are never fetched. Every git call runs with a fixed configuration and a minimal
environment (no system or global config, no hooks, only the https transport), so nothing
from the caller's environment or the target repository can add a hook, a diff driver, a
credential helper, or another transport (SH-16).
"""

import os
import re
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

from rootflake.sandbox.workspace import project_slug

CLONE_TIMEOUT_S = 1800
GIT_TIMEOUT_S = 300
MAX_BLOB = 1024 * 1024
MAX_ERROR = 500

_SHA_RE = re.compile(r"[0-9a-f]{40}")
_TREE_ENTRY_RE = re.compile(rb"(\d{6}) (\w+) ([0-9a-f]{40}) +(\d+|-)\t")
_MISSING = (b"not our ref", b"couldn't find remote ref", b"no such remote ref")

Run = Callable[[list[str], dict[str, str], float], subprocess.CompletedProcess]


class GitError(RuntimeError):
    pass


def git_config(protocols: Sequence[str]) -> list[str]:
    settings = [
        "protocol.allow=never",
        *(f"protocol.{name}.allow=always" for name in protocols),
        *([] if "file" in protocols else ["protocol.file.allow=never"]),
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "submodule.recurse=false",
        "fetch.recurseSubmodules=false",
        "gc.auto=0",
        "maintenance.auto=false",
        "credential.helper=",
    ]
    return [part for setting in settings for part in ("-c", setting)]


def git_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _run(args: list[str], env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, env=env, cwd="/", stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout
    )


def _error_text(stderr: bytes) -> str:
    text = stderr.decode("ascii", errors="replace")
    return "".join(c if c.isprintable() or c == "\n" else "?" for c in text)[-MAX_ERROR:]


def git_version(run: Run = _run) -> str:
    return run(["git", "--version"], git_env(), GIT_TIMEOUT_S).stdout.decode().strip()


def _check_sha(sha: str) -> None:
    if not _SHA_RE.fullmatch(sha):
        raise ValueError(f"commit must be a full lowercase SHA: {sha!r}")


def _check_path(path: str) -> None:
    parts = path.split("/")
    if path.startswith("-") or any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"path must be a plain relative path: {path!r}")


class GitRepo:
    def __init__(self, path: Path, protocols: Sequence[str] = ("https",), run: Run = _run):
        self.path = path
        self.protocols = tuple(protocols)
        self.run_git = run

    @classmethod
    def mirror(cls, project_url: str, dest: Path, run: Run = _run) -> GitRepo:
        """Bare clone of a GitHub project into an empty managed directory."""
        project_slug(project_url)
        return cls.clone(project_url, dest, ("https",), run)

    @classmethod
    def clone(
        cls, source: str, dest: Path, protocols: Sequence[str] = ("https",), run: Run = _run
    ) -> GitRepo:
        repo = cls(dest, protocols, run)
        args = ["clone", "--bare", "--no-recurse-submodules", "--quiet", "--", source, str(dest)]
        repo._git(args, CLONE_TIMEOUT_S, in_repo=False)
        return repo

    def _git(
        self, args: list[str], timeout: float = GIT_TIMEOUT_S, in_repo: bool = True
    ) -> subprocess.CompletedProcess:
        command = ["git", *git_config(self.protocols)]
        if in_repo:
            command += ["--git-dir", str(self.path)]
        try:
            result = self.run_git(command + args, git_env(), timeout)
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} timed out after {timeout} s") from None
        if result.returncode != 0:
            raise GitError(f"git {args[0]} failed: {_error_text(result.stderr)}")
        return result

    def _missing_on_remote(self, args: list[str]) -> bool:
        """Runs a fetch; True when the remote says the ref or commit does not exist."""
        try:
            self._git(args)
        except GitError as error:
            if any(text.decode() in str(error) for text in _MISSING):
                return True
            raise
        return False

    def _has_commit(self, sha: str) -> bool:
        try:
            self._git(["cat-file", "-e", f"{sha}^{{commit}}"])
        except GitError:
            return False
        return True

    def fetch_pr_head(self, number: int) -> str | None:
        if type(number) is not int or number < 1:
            raise ValueError(f"pull request number must be positive: {number!r}")
        ref = f"refs/rootflake/pull/{number}"
        fetch = ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "origin"]
        if self._missing_on_remote([*fetch, f"+refs/pull/{number}/head:{ref}"]):
            return None
        return self._git(["rev-parse", "--verify", f"{ref}^{{commit}}"]).stdout.decode().strip()

    def ensure_commit(self, sha: str) -> bool:
        _check_sha(sha)
        if self._has_commit(sha):
            return True
        fetch = ["fetch", "--quiet", "--no-tags", "--no-recurse-submodules", "origin", sha]
        if self._missing_on_remote(fetch):
            return False
        return self._has_commit(sha)

    def canonical_diff(self, head: str, base: str) -> bytes | None:
        """`git diff M H` with M the merge base of the PR head and its base commit (ADR 0006)."""
        if not (self.ensure_commit(head) and self.ensure_commit(base)):
            return None
        try:
            merge_base = self._git(["merge-base", head, base]).stdout.decode().strip()
        except GitError:
            return None
        _check_sha(merge_base)
        diff = ["diff", "--no-color", "--no-ext-diff", "--no-textconv", merge_base, head]
        return self._git(diff).stdout

    def read_blob(self, sha: str, path: str, limit: int = MAX_BLOB) -> bytes | None:
        """A regular file's bytes at a commit; None for links, gitlinks, missing, or oversized."""
        _check_sha(sha)
        _check_path(path)
        if not self._has_commit(sha):
            return None
        listing = self._git(["ls-tree", "-l", "-z", sha, "--", path]).stdout
        entry = _TREE_ENTRY_RE.match(listing)
        if not entry or listing.split(b"\0")[0].split(b"\t", 1)[1] != path.encode():
            return None
        mode, kind, obj, size = entry.groups()
        if mode not in (b"100644", b"100755") or kind != b"blob" or int(size) > limit:
            return None
        data = self._git(["cat-file", "blob", obj.decode()]).stdout
        return data if len(data) <= limit else None
