import shutil
import tempfile
import time
import typing
from pathlib import Path

import pytest

from rootflake.sandbox import download as download_module
from rootflake.sandbox.container import RunSpec, SandboxLimits
from rootflake.sandbox.download import (
    DockerCli,
    DockerOps,
    DownloadOutcome,
    build_proxy_args,
    check_proxy_args,
    download_dependencies,
)
from rootflake.sandbox.executor import ProcResult

SANDBOX = "rootflake-sandbox@sha256:" + "a" * 64
PROXY = "rootflake-proxy@sha256:" + "b" * 64
ROOT = Path(tempfile.mkdtemp(prefix="rootflake-dl-test-")).resolve()
(ROOT / "ws").mkdir()
(ROOT / "m2").mkdir()
(ROOT / "pristine").mkdir()
(ROOT / "ws" / "inner").mkdir()
(ROOT / "pristine" / "ws").mkdir()
(ROOT / "pristine" / "pom.xml").write_bytes(
    b"<project><repositories><repository><url>https://jitpack.io</url></repository>"
    b"</repositories></project>"
)


def spec(workspace=ROOT / "ws"):
    return RunSpec(
        image=SANDBOX,
        workspace=workspace,
        maven_repo=ROOT / "m2",
        jdk=17,
        command=["placeholder"],
        name="rootflake-00000000",
    )


DENIED_LOG = ProcResult(
    0, b"", b'{"event": "denied", "host": "evil.example", "port": 443}\n', False
)


class FakeDocker:
    def __init__(
        self, download_result=None, fail_on=None, members=None, raise_on=None, logs=DENIED_LOG
    ):
        self.members = members
        self.commands = []
        self.download_result = download_result or ProcResult(0, b"BUILD SUCCESS", b"", False)
        self.fail_on = fail_on
        self.raise_on = raise_on
        self.logs = logs
        self.stopped = []
        self.proxy_starts = []

    def docker(self, argv):
        self.commands.append(argv)
        if self.raise_on and argv[: len(self.raise_on)] == self.raise_on:
            raise RuntimeError(f"docker hung: {argv[:3]}")
        if self.fail_on and argv[:3] == self.fail_on:
            return ProcResult(1, b"", b"boom", False)
        if argv[:3] == ["docker", "network", "inspect"]:
            members = self.members if self.members is not None else [self.proxy_name()]
            return ProcResult(0, "\n".join(members).encode(), b"", False)
        if argv[:2] == ["docker", "logs"]:
            return self.logs
        return ProcResult(0, b"", b"", False)

    def start_proxy(self, args, name, egress, image):
        self.proxy_starts.append((name, egress, image))
        return self.docker(args)

    def proxy_name(self):
        run = next(c for c in self.commands if c[:3] == ["docker", "run", "-d"])
        return run[run.index("--name") + 1]

    def run(self, args, timeout_s):
        self.commands.append(args)
        return self.download_result

    def stop(self, name):
        self.stopped.append(name)


def go(fake):
    return download_dependencies(
        spec(), SandboxLimits(), fake, (ROOT,), ROOT / "pristine", proxy_image=PROXY, timeout_s=1800
    )


def creates(fake):
    return [c for c in fake.commands if c[:3] == ["docker", "network", "create"]]


def names(fake):
    internal = next(c[-1] for c in creates(fake) if "--internal" in c)
    return internal, fake.proxy_name()


def test_successful_download_runs_the_steps_in_order_and_tears_down():
    fake = FakeDocker()
    record = go(fake)
    assert record.outcome is DownloadOutcome.OK
    internal, proxy = names(fake)
    egress = next(c[-1] for c in creates(fake) if "--internal" not in c)
    kinds = [" ".join(c[:3]) for c in fake.commands]
    assert kinds[:4] == [
        "docker network create",
        "docker network create",
        "docker run -d",
        "docker network connect",
    ]
    assert fake.commands[3] == ["docker", "network", "connect", internal, proxy]
    assert kinds[4] == "docker network inspect"
    download = fake.commands[5]
    assert download[download.index("--network") + 1] == internal
    assert download[-1] == "rootflake-fetch"
    assert ["docker", "rm", "-f", proxy] in fake.commands
    assert fake.commands[-2:] == [
        ["docker", "network", "rm", internal],
        ["docker", "network", "rm", egress],
    ]


def test_both_networks_are_created_ipv4_only_and_the_inner_one_internal():
    fake = FakeDocker()
    go(fake)
    internal, egress = sorted(creates(fake), key=lambda c: "--internal" not in c)
    assert "--internal" in internal and "--internal" not in egress
    for create in (internal, egress):
        assert "--ipv6=false" in create
        assert create[create.index("--driver") + 1] == "bridge"


def test_proxy_gets_the_pristine_allowlist_and_runs_locked_down():
    fake = FakeDocker()
    record = go(fake)
    proxy_run = next(c for c in fake.commands if c[:3] == ["docker", "run", "-d"])
    allowed = {proxy_run[i + 1] for i, a in enumerate(proxy_run) if a == "--allow"}
    assert allowed == {"repo.maven.apache.org", "repo1.maven.org", "jitpack.io"}
    assert record.allowlist == allowed
    egress = next(c[-1] for c in creates(fake) if "--internal" not in c)
    assert proxy_run[proxy_run.index("--network") + 1] == egress
    for flag in ["--read-only", "--init"]:
        assert flag in proxy_run
    assert "--rm" not in proxy_run
    assert proxy_run[proxy_run.index("--cap-drop") + 1] == "ALL"
    assert proxy_run[proxy_run.index("--user") + 1] == "10002:10002"
    assert "--mount" not in proxy_run and "-v" not in proxy_run and "-p" not in proxy_run
    assert PROXY in proxy_run


def test_an_unexpected_member_on_the_internal_network_stops_the_download():
    fake = FakeDocker(members=["rootflake-px-0000000000000000", "stranger"])
    record = go(fake)
    assert record.outcome is DownloadOutcome.INFRASTRUCTURE
    assert not any("rootflake-fetch" in c for c in fake.commands)
    internal, _ = names(fake)
    assert ["docker", "network", "rm", internal] in fake.commands


def test_denied_proxy_requests_are_recorded():
    record = go(FakeDocker())
    assert record.denied == [("evil.example", 443)]


def test_failed_download_is_recorded_and_still_torn_down():
    fake = FakeDocker(download_result=ProcResult(1, b"BUILD FAILURE", b"", False))
    record = go(fake)
    assert record.outcome is DownloadOutcome.FAILED
    net, proxy = names(fake)
    assert ["docker", "network", "rm", net] in fake.commands


def test_timeout_stops_the_download_container():
    fake = FakeDocker(download_result=ProcResult(-1, b"", b"", True))
    record = go(fake)
    assert record.outcome is DownloadOutcome.TIMEOUT
    assert len(fake.stopped) == 1


def test_a_failing_setup_step_still_tears_down_what_exists():
    fake = FakeDocker(fail_on=["docker", "network", "connect"])
    record = go(fake)
    assert record.outcome is DownloadOutcome.INFRASTRUCTURE
    net, proxy = names(fake)
    assert ["docker", "rm", "-f", proxy] in fake.commands
    assert ["docker", "network", "rm", net] in fake.commands
    assert not any(c[:2] == ["docker", "run"] and "rootflake-fetch" in c for c in fake.commands)


def test_failed_network_create_runs_nothing_else():
    fake = FakeDocker(fail_on=["docker", "network", "create"])
    record = go(fake)
    assert record.outcome is DownloadOutcome.INFRASTRUCTURE
    assert len(fake.commands) == 1


@pytest.mark.parametrize("timeout_s", [0, 4 * 3600])
def test_timeout_is_bounded(timeout_s):
    with pytest.raises(ValueError):
        download_dependencies(
            spec(),
            SandboxLimits(),
            FakeDocker(),
            (ROOT,),
            ROOT / "pristine",
            proxy_image=PROXY,
            timeout_s=timeout_s,
        )


EGRESS = "rootflake-eg-0123456789ab"
PX = "rootflake-px-0123456789ab"


def proxy_args(hosts=("repo1.maven.org",)):
    return build_proxy_args(PX, EGRESS, PROXY, sorted(hosts))


def test_proxy_args_pass_their_check():
    check_proxy_args(proxy_args(), PX, EGRESS, PROXY)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a[:3] + ["--mount", "type=bind,src=/,dst=/h"] + a[3:],
        lambda a: a[:3] + ["-p", "3128:3128"] + a[3:],
        lambda a: a[:3] + ["--privileged"] + a[3:],
        lambda a: [x for x in a if x != "--read-only"],
        lambda a: [("bridge" if x == EGRESS else x) for x in a],
        lambda a: [("host" if x == EGRESS else x) for x in a],
        lambda a: [("0:0" if x == "10002:10002" else x) for x in a],
        lambda a: [("rootflake-proxy:dev" if x == PROXY else x) for x in a],
        lambda a: a + ["--allow", "*.evil.com"],
        lambda a: a + ["--allow-private"],
        lambda a: [x for x in a if x != "--allow"][:-1],
        lambda a: a[:3] + ["--rm"] + a[3:],
    ],
)
def test_proxy_check_rejects_anything_else(mutate):
    with pytest.raises(ValueError):
        check_proxy_args(mutate(proxy_args()), PX, EGRESS, PROXY)


def test_proxy_builder_rejects_bad_hosts_and_names():
    with pytest.raises(ValueError):
        build_proxy_args(PX, EGRESS, PROXY, ["evil .com"])
    with pytest.raises(ValueError):
        build_proxy_args("px", EGRESS, PROXY, ["repo1.maven.org"])
    with pytest.raises(ValueError):
        build_proxy_args(PX, "bridge", PROXY, ["repo1.maven.org"])
    with pytest.raises(ValueError):
        build_proxy_args(PX, EGRESS, PROXY, [])


def removed_everything(fake):
    created = [c[-1] for c in creates(fake)]
    removed = [c[-1] for c in fake.commands if c[:3] == ["docker", "network", "rm"]]
    return sorted(created) == sorted(removed) and ["docker", "rm", "-f", fake.proxy_name()] in (
        fake.commands
    )


def test_a_failing_log_fetch_does_not_skip_the_teardown():
    fake = FakeDocker(raise_on=["docker", "logs"])
    record = go(fake)
    assert removed_everything(fake)
    assert any("logs" in error for error in record.cleanup_errors)


def test_a_failing_log_parse_does_not_skip_the_teardown(monkeypatch):
    def broken(log):
        raise RuntimeError("parser broke")

    monkeypatch.setattr(download_module, "_denied", broken)
    fake = FakeDocker()
    record = go(fake)
    assert removed_everything(fake)
    assert any("parser broke" in error for error in record.cleanup_errors)


def test_a_raising_proxy_removal_still_removes_both_networks():
    fake = FakeDocker(raise_on=["docker", "rm", "-f"])
    record = go(fake)
    assert len([c for c in fake.commands if c[:3] == ["docker", "network", "rm"]]) == 2
    assert any(fake.proxy_name() in error for error in record.cleanup_errors)


def test_a_raising_network_removal_still_removes_the_other_network():
    fake = FakeDocker(raise_on=["docker", "network", "rm"])
    record = go(fake)
    assert len([c for c in fake.commands if c[:3] == ["docker", "network", "rm"]]) == 2
    assert len(record.cleanup_errors) == 2


def test_failed_removals_are_recorded():
    fake = FakeDocker(fail_on=["docker", "rm", "-f"])
    assert any(fake.proxy_name() in e for e in go(fake).cleanup_errors)
    fake = FakeDocker(fail_on=["docker", "network", "rm"])
    errors = go(fake).cleanup_errors
    for network in (c[-1] for c in creates(fake)):
        assert any(network in e for e in errors)


def test_malformed_denial_events_are_skipped():
    log = (
        b'{"event": "denied", "host": "a.example", "port": "x"}\n'
        b'{"event": "denied"}\n'
        b'{"event": "denied", "host": 5, "port": 443}\n'
        b"[1, 2]\n"
        b'{"event": "denied", "host": "ok.example", "port": 443}\n'
    )
    record = go(FakeDocker(logs=ProcResult(0, b"", log, False)))
    assert record.denied == [("ok.example", 443)]
    assert record.cleanup_errors == []


def test_a_proxy_that_fails_to_start_is_still_removed():
    fake = FakeDocker(fail_on=["docker", "run", "-d"])
    record = go(fake)
    assert record.outcome is DownloadOutcome.INFRASTRUCTURE
    assert ["docker", "rm", "-f", fake.proxy_name()] in fake.commands


def test_a_truncated_proxy_log_is_flagged():
    assert go(FakeDocker()).proxy_log_truncated is False
    logs = ProcResult(0, b"", b"{}", False, stderr_dropped=10)
    assert go(FakeDocker(logs=logs)).proxy_log_truncated is True


def test_download_output_is_stripped_of_terminal_controls():
    fake = FakeDocker(download_result=ProcResult(0, b"\x1b[31mBUILD\x1b]0;t\x07 OK", b"", False))
    assert go(fake).output == "BUILD OK"


def test_tampered_download_args_are_refused(monkeypatch):
    real = download_module.build_download_args

    def tampered(*args, **kwargs):
        return [*real(*args, **kwargs)[:2], "--privileged", *real(*args, **kwargs)[2:]]

    monkeypatch.setattr(download_module, "build_download_args", tampered)
    fake = FakeDocker()
    with pytest.raises(ValueError):
        go(fake)
    assert not any("rootflake-fetch" in c for c in fake.commands)
    assert removed_everything(fake)


def test_tampered_proxy_args_are_refused(monkeypatch):
    real = download_module.build_proxy_args
    calls = []

    def tampered_once(*args, **kwargs):
        built = real(*args, **kwargs)
        calls.append(built)
        return built[:3] + ["--privileged"] + built[3:] if len(calls) == 1 else built

    monkeypatch.setattr(download_module, "build_proxy_args", tampered_once)
    fake = FakeDocker()
    with pytest.raises(ValueError):
        go(fake)
    assert not any(c[:3] == ["docker", "run", "-d"] for c in fake.commands)


@pytest.mark.parametrize(
    "workspace",
    [ROOT / "pristine", ROOT / "pristine" / "ws"],
    ids=["same directory", "workspace inside pristine"],
)
def test_workspace_and_pristine_must_be_separate(workspace):
    fake = FakeDocker()
    with pytest.raises(ValueError, match="pristine"):
        download_dependencies(
            spec(workspace), SandboxLimits(), fake, (ROOT,), ROOT / "pristine", PROXY, 1800
        )
    assert fake.commands == []


def test_pristine_inside_the_workspace_is_refused():
    fake = FakeDocker()
    with pytest.raises(ValueError, match="pristine"):
        download_dependencies(
            spec(), SandboxLimits(), fake, (ROOT,), ROOT / "ws" / "inner", PROXY, 1800
        )
    assert fake.commands == []


def test_docker_ops_is_a_protocol():
    assert typing.is_protocol(DockerOps)


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "exec", "rootflake-px-0123456789ab", "sh"],
        ["docker", "run", "--rm", "alpine"],
        ["docker", "run", "-d", "--name", "rootflake-px-0123456789ab", PROXY],
        ["docker", "network", "ls"],
        ["docker", "cp", "a", "b"],
        ["sh", "-c", "docker logs x"],
        ["docker"],
        [],
    ],
)
def test_docker_cli_refuses_other_subcommands(argv):
    with pytest.raises(ValueError):
        DockerCli().docker(argv)


def test_docker_cli_runs_with_a_two_minute_timeout(monkeypatch):
    seen = []
    monkeypatch.setattr(DockerCli, "run", lambda self, args, timeout_s: seen.append(timeout_s))
    DockerCli().docker(["docker", "logs", "rootflake-px-0123456789ab"])
    assert seen == [120]


def test_docker_cli_times_out_a_hung_command(monkeypatch, tmp_path):
    fake = tmp_path / "docker"
    fake.write_text(f"#!/bin/sh\nexec {shutil.which('sleep')} 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(download_module, "DOCKER_TIMEOUT_S", 0.5)
    started = time.monotonic()
    result = DockerCli().docker(["docker", "logs", "rootflake-px-0123456789ab"])
    assert result.timed_out
    assert time.monotonic() - started < 5


def download_name(fake):
    run = next(c for c in fake.commands if "rootflake-fetch" in c)
    return run[run.index("--name") + 1]


def test_the_proxy_is_started_through_start_proxy():
    fake = FakeDocker()
    go(fake)
    internal, proxy = names(fake)
    egress = next(c[-1] for c in creates(fake) if "--internal" not in c)
    assert fake.proxy_starts == [(proxy, egress, PROXY)]


def test_the_download_container_is_removed_afterwards():
    fake = FakeDocker()
    record = go(fake)
    assert ["docker", "rm", "-f", download_name(fake)] in fake.commands
    assert record.cleanup_errors == []


def test_a_failed_download_container_removal_is_recorded():
    fake = FakeDocker(fail_on=["docker", "rm", "-f"])
    errors = go(fake).cleanup_errors
    assert any(download_name(fake) in e for e in errors)


def test_a_raising_download_container_removal_does_not_skip_the_rest():
    fake = FakeDocker(raise_on=["docker", "rm", "-f"])
    record = go(fake)
    assert any(download_name(fake) in e for e in record.cleanup_errors)
    assert len([c for c in fake.commands if c[:3] == ["docker", "network", "rm"]]) == 2


def test_no_download_container_removal_when_none_was_started():
    fake = FakeDocker(fail_on=["docker", "network", "connect"])
    go(fake)
    removals = [c for c in fake.commands if c[:3] == ["docker", "rm", "-f"]]
    assert removals == [["docker", "rm", "-f", fake.proxy_name()]]


def _proxy_args():
    return build_proxy_args(PX, EGRESS, PROXY, ["repo1.maven.org"])


def test_docker_cli_start_proxy_runs_checked_args(monkeypatch):
    seen = []
    monkeypatch.setattr(
        DockerCli, "run", lambda self, args, timeout_s: seen.append((args, timeout_s))
    )
    DockerCli().start_proxy(_proxy_args(), PX, EGRESS, PROXY)
    assert seen == [(_proxy_args(), 120)]


@pytest.mark.parametrize(
    "args",
    [
        _proxy_args()[:3] + ["--privileged"] + _proxy_args()[3:],
        [x for x in _proxy_args() if x != "--read-only"],
        _proxy_args() + ["--allow-private"],
    ],
)
def test_docker_cli_start_proxy_refuses_anything_else(monkeypatch, args):
    seen = []
    monkeypatch.setattr(DockerCli, "run", lambda self, a, timeout_s: seen.append(a))
    with pytest.raises(ValueError):
        DockerCli().start_proxy(args, PX, EGRESS, PROXY)
    assert seen == []
