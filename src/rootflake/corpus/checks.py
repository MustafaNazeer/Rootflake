"""The real rule 9 to 12 checks for the walk: GitHub metadata, the canonical diff, the POM level.

Rules 11 and 12 need sandbox builds of target projects, which wait until the network probe,
the gVisor measurement, and the in sandbox tests exist; until then they refuse to run.
"""

import hashlib
from collections.abc import Callable

from rootflake.corpus.github import PullInfo, fetch_pr, parse_pr_url
from rootflake.corpus.gitrepo import GitError, GitRepo
from rootflake.corpus.idoft import Row
from rootflake.corpus.javalevel import java_level
from rootflake.corpus.walk import FixDiff, TransientError, Unavailable

_NOT_FOUND = ("Repository not found", "repository not found")


def module_poms(module_path: str) -> list[str] | None:
    """POM paths from the module up to the repository root, or None for an unsafe path."""
    if module_path in (".", ""):
        return ["pom.xml"]
    parts = module_path.rstrip("/").split("/")
    if module_path.startswith(("/", "-")) or any(p in ("", ".", "..") for p in parts):
        return None
    return ["/".join([*parts[:depth], "pom.xml"]) for depth in range(len(parts), -1, -1)]


class CorpusChecks:
    def __init__(
        self,
        token: str,
        fetch: Callable[[str, str, int, str], PullInfo | Unavailable] = fetch_pr,
        mirror: Callable[[str], GitRepo] | None = None,
    ):
        if mirror is None:
            raise ValueError("a mirror factory bound to the managed root is required")
        self._token = token
        self._fetch = fetch
        self._mirror = mirror
        self._repos: dict[str, GitRepo | Unavailable] = {}

    def _repo(self, project_url: str) -> GitRepo | Unavailable:
        key = project_url.lower()
        if key not in self._repos:
            try:
                self._repos[key] = self._mirror(project_url)
            except GitError as error:
                if not any(text in str(error) for text in _NOT_FOUND):
                    raise TransientError(f"cannot mirror {project_url}: {error}") from None
                self._repos[key] = Unavailable("repository cannot be cloned")
        return self._repos[key]

    def fix_diff(self, row: Row) -> FixDiff | Unavailable:
        owner, name, number = parse_pr_url(row.project_url, row.pr_url)
        info = self._fetch(owner, name, number, self._token)
        if isinstance(info, Unavailable):
            return info
        repo = self._repo(row.project_url)
        if isinstance(repo, Unavailable):
            return repo
        try:
            head = repo.fetch_pr_head(number)
            if head is None:
                return Unavailable("pull request head cannot be fetched")
            if head != info.head_sha:
                return Unavailable("pull ref head differs from the API head")
            diff = repo.canonical_diff(info.head_sha, info.base_sha)
        except GitError as error:
            raise TransientError(f"git failed for {row.pr_url}: {error}") from None
        if diff is None:
            return Unavailable("canonical diff cannot be computed")
        digest = hashlib.sha256(diff).hexdigest()
        return FixDiff(info.head_sha, info.merge_commit_sha, info.merged_at, digest)

    def java_level(self, row: Row) -> int | None:
        paths = module_poms(row.module_path)
        repo = self._repo(row.project_url)
        if paths is None or isinstance(repo, Unavailable):
            return None
        try:
            if not repo.ensure_commit(row.sha_detected):
                return None
            poms: list[bytes | None] = [repo.read_blob(row.sha_detected, p) for p in paths]
        except GitError as error:
            raise TransientError(f"git failed for {row.project_url}: {error}") from None
        return java_level(poms)

    def build(self, row: Row) -> int | None:
        raise NotImplementedError("rule 11 needs sandbox builds of target projects")

    def runnable(self, row: Row, jdk: int) -> bool:
        raise NotImplementedError("rule 12 needs sandbox runs of target tests")
