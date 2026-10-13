"""Builds a project's download allowlist from its POM files, read on the host.

Nothing from the project is executed: every pom.xml in the workspace is parsed as untrusted
XML, and only https repository URLs on port 443 with a plain host name are kept. Anything
else is recorded as rejected, so a project that needs it fails to download and is counted
as a build failure (docs/adr/0005-local-sandbox-hardening.md, Decision 3).
"""

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from xml.parsers import expat

from rootflake.sandbox.proxy import HOST_RE
from rootflake.sandbox.reports import read_report
from rootflake.sandbox.xmlsafe import RefusedXml, new_parser

CENTRAL_HOSTS = frozenset({"repo.maven.apache.org", "repo1.maven.org"})
MAX_POM_BYTES = 1024 * 1024
MAX_POMS = 2000
MAX_EXTRA_HOSTS = 10
REPOSITORY_PATHS = (
    ("repositories", "repository", "url"),
    ("pluginRepositories", "pluginRepository", "url"),
)


@dataclass(frozen=True)
class ProjectAllowlist:
    hosts: frozenset[str]
    rejected: list[str]


def _host_of(url: str) -> str | None:
    if "${" in url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or parts.username or parts.password or port not in (None, 443):
        return None
    if "." not in host or not HOST_RE.fullmatch(host):
        return None
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        return host


def repository_hosts(pom: bytes) -> tuple[set[str], list[str]]:
    stack: list[str] = []
    urls: list[str] = []
    text: list[str] = []

    def start(name: str, _attrs: dict[str, str]) -> None:
        stack.append(name)
        text.clear()

    def data(chunk: str) -> None:
        text.append(chunk)

    def end(_name: str) -> None:
        if tuple(stack[-3:]) in REPOSITORY_PATHS:
            urls.append("".join(text).strip())
        stack.pop()
        text.clear()

    parser = new_parser()
    parser.StartElementHandler = start
    parser.CharacterDataHandler = data
    parser.EndElementHandler = end
    try:
        parser.Parse(pom, True)
    except (RefusedXml, expat.ExpatError) as error:
        raise ValueError(f"unreadable POM: {error}") from error
    hosts, rejected = set(), []
    for url in urls:
        host = _host_of(url)
        if host is None:
            rejected.append(url)
        else:
            hosts.add(host)
    return hosts, rejected


def project_allowlist(workspace: Path) -> ProjectAllowlist:
    hosts, rejected = set(CENTRAL_HOSTS), []
    poms = 0
    for directory, dirnames, filenames in os.walk(workspace, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        if "pom.xml" not in filenames:
            continue
        poms += 1
        if poms > MAX_POMS:
            raise ValueError(f"more than {MAX_POMS} POM files")
        relative = Path(directory).relative_to(workspace) / "pom.xml"
        data = read_report(workspace, relative, limit=MAX_POM_BYTES)
        if data is None or len(data) > MAX_POM_BYTES:
            rejected.append(f"unreadable or oversized: {relative}")
            continue
        try:
            found, refused = repository_hosts(data)
        except ValueError:
            rejected.append(f"unparseable: {relative}")
            continue
        for host in sorted(found - hosts):
            if len(hosts - CENTRAL_HOSTS) >= MAX_EXTRA_HOSTS:
                rejected.append(f"too many repository hosts: {host}")
            else:
                hosts.add(host)
        rejected += refused
    return ProjectAllowlist(frozenset(hosts), rejected)
