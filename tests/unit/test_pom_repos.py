import os
from pathlib import Path

import pytest

from rootflake.sandbox import pom_repos
from rootflake.sandbox.pom_repos import CENTRAL_HOSTS, project_allowlist, repository_hosts

POM = b"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <repositories>
    <repository><id>a</id><url>https://repo.spring.io/release</url></repository>
    <repository><id>b</id><url>http://insecure.example.org/maven2</url></repository>
  </repositories>
  <pluginRepositories>
    <pluginRepository><id>c</id><url>https://Plugins.Example.COM:443/m2</url></pluginRepository>
  </pluginRepositories>
  <distributionManagement>
    <repository><id>d</id><url>https://deploy.example.net/</url></repository>
  </distributionManagement>
  <profiles><profile><repositories>
    <repository><id>e</id><url>https://profile.example.io/m2</url></repository>
  </repositories></profile></profiles>
</project>"""


def test_https_repositories_and_plugin_repositories_are_collected():
    hosts, rejected = repository_hosts(POM)
    assert hosts == {"repo.spring.io", "plugins.example.com", "profile.example.io"}
    assert rejected == ["http://insecure.example.org/maven2"]


def test_distribution_management_is_ignored():
    hosts, _ = repository_hosts(POM)
    assert "deploy.example.net" not in hosts


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pass@host.example.com/m2",
        "https://host.example.com:8443/m2",
        "https://10.0.0.5/m2",
        "https://localhost/m2",
        "https://${repo.host}/m2",
        "file:///etc",
        "https:///nohost",
    ],
)
def test_unusable_urls_are_rejected_not_allowlisted(url):
    pom = (
        f"<project><repositories><repository><url>{url}</url></repository></repositories></project>"
    )
    hosts, rejected = repository_hosts(pom.encode())
    assert hosts == set()
    assert rejected == [url]


def test_a_pom_with_a_dtd_is_unreadable():
    with pytest.raises(ValueError):
        repository_hosts(b'<?xml version="1.0"?><!DOCTYPE p [<!ENTITY x "y">]><project/>')


def test_project_allowlist_walks_every_pom_and_always_includes_central(tmp_path):
    (tmp_path / "pom.xml").write_bytes(POM)
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "pom.xml").write_bytes(
        b"<project><repositories><repository><url>https://jitpack.io</url></repository>"
        b"</repositories></project>"
    )
    allowlist = project_allowlist(tmp_path)
    assert CENTRAL_HOSTS <= allowlist.hosts
    assert {"repo.spring.io", "jitpack.io"} <= allowlist.hosts
    assert allowlist.rejected == ["http://insecure.example.org/maven2"]


def test_project_allowlist_skips_symlinked_poms_and_directories(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pom.xml").write_bytes(
        b"<project><repositories><repository><url>https://evil.example.com</url></repository>"
        b"</repositories></project>"
    )
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "pom.xml").write_bytes(b"<project/>")
    os.symlink(outside, ws / "linked")
    (ws / "other.xml").symlink_to(outside / "pom.xml")
    assert "evil.example.com" not in project_allowlist(ws).hosts


def test_project_allowlist_caps_the_number_of_extra_hosts(tmp_path):
    urls = "".join(
        f"<repository><url>https://r{i}.example.com</url></repository>" for i in range(15)
    )
    (tmp_path / "pom.xml").write_text(f"<project><repositories>{urls}</repositories></project>")
    allowlist = project_allowlist(tmp_path)
    assert len(allowlist.hosts - CENTRAL_HOSTS) == 10
    assert sum("too many repository hosts" in r for r in allowlist.rejected) == 5


def _pom(url):
    return (
        f"<project><repositories><repository><url>{url}</url></repository></repositories></project>"
    ).encode()


def _modules(root, count):
    for i in range(count):
        (root / f"m{i}").mkdir()
        (root / f"m{i}" / "pom.xml").write_bytes(b"<project/>")


def test_the_pom_count_limit_is_inclusive(tmp_path, monkeypatch):
    monkeypatch.setattr(pom_repos, "MAX_POMS", 3)
    _modules(tmp_path, 3)
    project_allowlist(tmp_path)
    (tmp_path / "m3").mkdir()
    (tmp_path / "m3" / "pom.xml").write_bytes(b"<project/>")
    with pytest.raises(ValueError, match="more than 3"):
        project_allowlist(tmp_path)


def test_a_pom_over_the_size_limit_is_rejected_and_one_at_the_limit_is_read(tmp_path, monkeypatch):
    at_limit = _pom("https://fits.example.com")
    monkeypatch.setattr(pom_repos, "MAX_POM_BYTES", len(at_limit))
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "pom.xml").write_bytes(at_limit)
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "pom.xml").write_bytes(_pom("https://toolong.example.com"))
    allowlist = project_allowlist(tmp_path)
    assert "fits.example.com" in allowlist.hosts
    assert "toolong.example.com" not in allowlist.hosts
    assert allowlist.rejected == [f"unreadable or oversized: {Path('b/pom.xml')}"]


def test_poms_under_git_are_not_read(tmp_path):
    (tmp_path / "pom.xml").write_bytes(b"<project/>")
    (tmp_path / ".git" / "refs").mkdir(parents=True)
    (tmp_path / ".git" / "refs" / "pom.xml").write_bytes(_pom("https://hidden.example.com"))
    assert "hidden.example.com" not in project_allowlist(tmp_path).hosts
