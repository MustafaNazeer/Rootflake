"""Pull request metadata for rule 9 from the GitHub REST API.

Only a definitive answer about the pull request (not found, or not merged) becomes a rule
outcome. Rate limits, server errors, network errors, and malformed responses raise
`TransientError`, which aborts the walk instead of silently changing the draw. The token is
read from `gh auth token` at run time, sent only in the Authorization header, and never put
in a URL, an exception message, or a log record.
"""

import json
import re
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from rootflake.corpus.walk import TransientError, Unavailable

API_URL = "https://api.github.com"
TIMEOUT_S = 30
MAX_BODY = 4 * 1024 * 1024

_PR_RE = re.compile(r"https://github\.com/([A-Za-z0-9-]+)/([A-Za-z0-9._-]+)/pull/([1-9][0-9]*)")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")

Opener = Callable[[str, dict[str, str], float], tuple[int, bytes]]


@dataclass(frozen=True)
class PullInfo:
    merged_at: str
    merge_commit_sha: str
    head_sha: str
    base_sha: str


def parse_pr_url(project_url: str, pr_url: str) -> tuple[str, str, int]:
    match = _PR_RE.fullmatch(pr_url)
    if not match or match.group(2) in (".", ".."):
        raise ValueError(f"not a GitHub pull request link: {pr_url!r}")
    owner, repo, number = match.groups()
    if f"https://github.com/{owner}/{repo}".lower() != project_url.lower():
        raise ValueError(f"{pr_url!r} is not a pull request of {project_url!r}")
    return owner.lower(), repo.lower(), int(number)


def token_from_gh(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> str:
    result = run(["gh", "auth", "token"], capture_output=True, text=True, timeout=30)
    token = result.stdout.strip() if result.returncode == 0 else ""
    if not token:
        raise RuntimeError("no GitHub token: run `gh auth login` first")
    return token


def _urlopen(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(MAX_BODY + 1)
    except urllib.error.HTTPError as error:
        return error.code, b""


def fetch_pr(
    owner: str, repo: str, number: int, token: str, opener: Opener = _urlopen
) -> PullInfo | Unavailable:
    url = f"{API_URL}/repos/{owner}/{repo}/pulls/{number}"
    headers = {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {token}"}
    try:
        status, body = opener(url, headers, TIMEOUT_S)
    except OSError, TimeoutError:
        raise TransientError(f"network error fetching {owner}/{repo} pull {number}") from None
    if status == 404:
        return Unavailable("pull request not found")
    if status != 200:
        raise TransientError(f"GitHub API returned {status} for {owner}/{repo} pull {number}")
    try:
        if len(body) > MAX_BODY:
            raise ValueError("oversized")
        data = json.loads(body)
        if data["merged"] is not True:
            return Unavailable("pull request not merged")
        info = PullInfo(
            merged_at=data["merged_at"],
            merge_commit_sha=data["merge_commit_sha"],
            head_sha=data["head"]["sha"],
            base_sha=data["base"]["sha"],
        )
        shas = (info.merge_commit_sha, info.head_sha, info.base_sha)
        if not all(isinstance(s, str) and _SHA_RE.fullmatch(s) for s in shas):
            raise ValueError("bad SHA")
        if not (isinstance(info.merged_at, str) and _TIME_RE.fullmatch(info.merged_at)):
            raise ValueError("bad merge time")
    except ValueError, KeyError, TypeError:
        raise TransientError(
            f"malformed GitHub API response for {owner}/{repo} pull {number}"
        ) from None
    return info
