"""Fills a project's Maven repository behind the allowlist proxy, then tears everything down.

Each download gets two fresh IPv4 only bridge networks: an internal one holding only the
download container and the proxy, and an outward one holding only the proxy. The allowlist
is read from the pristine checkout before any container has touched a copy of it
(docs/adr/0005-local-sandbox-hardening.md, Decision 3). Every network and the proxy are
removed afterwards whatever happened, and any cleanup failure is recorded.
"""

import json
import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Protocol

from rootflake.sandbox.container import (
    Download,
    RunSpec,
    SandboxLimits,
    build_download_args,
    check_args,
)
from rootflake.sandbox.executor import DockerRunner, ProcessRunner, ProcResult, result_output
from rootflake.sandbox.pom_repos import project_allowlist
from rootflake.sandbox.proxy import HOST_RE

PROXY_PORT = 3128
PROXY_USER = "10002:10002"
MAX_DOWNLOAD_S = 3 * 3600
PX_RE = re.compile(r"rootflake-px-[a-z0-9]{8,40}")
EGRESS_RE = re.compile(r"rootflake-eg-[a-z0-9]{8,40}")
DIGEST_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}")
FETCH_COMMAND = "rootflake-fetch"
DOCKER_TIMEOUT_S = 120
DOCKER_SUBCOMMANDS = (
    ("network", "create"),
    ("network", "connect"),
    ("network", "inspect"),
    ("network", "rm"),
    ("logs",),
    ("rm", "-f"),
)


class DockerOps(ProcessRunner, Protocol):
    def docker(self, argv: list[str]) -> ProcResult: ...

    def start_proxy(self, args: list[str], name: str, egress: str, image: str) -> ProcResult: ...


class DockerCli(DockerRunner):
    """Runs the few docker commands a download needs, each bounded in time and output."""

    def docker(self, argv: list[str]) -> ProcResult:
        if argv[:1] != ["docker"] or not any(
            tuple(argv[1 : 1 + len(sub)]) == sub for sub in DOCKER_SUBCOMMANDS
        ):
            raise ValueError(f"docker command not allowed here: {argv[:3]}")
        return self.run(argv, DOCKER_TIMEOUT_S)

    def start_proxy(self, args: list[str], name: str, egress: str, image: str) -> ProcResult:
        check_proxy_args(args, name, egress, image)
        return self.run(args, DOCKER_TIMEOUT_S)


class DownloadOutcome(Enum):
    OK = "ok"
    FAILED = "failed"
    TIMEOUT = "timeout"
    INFRASTRUCTURE = "infrastructure"


@dataclass(frozen=True)
class DownloadRecord:
    outcome: DownloadOutcome
    allowlist: set[str]
    rejected: list[str]
    denied: list[tuple[str, int]]
    exit_code: int | None
    output: str
    cleanup_errors: list[str]
    proxy_log_truncated: bool = False


def build_proxy_args(name: str, egress: str, image: str, hosts: Sequence[str]) -> list[str]:
    if not PX_RE.fullmatch(name) or not EGRESS_RE.fullmatch(egress):
        raise ValueError(f"bad proxy name or egress network: {name!r} {egress!r}")
    if not DIGEST_RE.fullmatch(image):
        raise ValueError(f"proxy image must be pinned by digest: {image!r}")
    if not hosts or any(not HOST_RE.fullmatch(host) for host in hosts):
        raise ValueError(f"allowlist must be non empty plain host names: {list(hosts)}")
    args = [
        "docker", "run", "-d",
        "--name", name,
        "--network", egress,
        "--user", PROXY_USER,
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",
        "--init",
        "--memory", "256m", "--memory-swap", "256m",
        "--cpus", "1",
        "--pids-limit", "64",
        "--log-opt", "max-size=10m", "--log-opt", "max-file=1",
        image,
        "--port", str(PROXY_PORT),
    ]  # fmt: skip
    for host in hosts:
        args += ["--allow", host]
    return args


def check_proxy_args(args: list[str], name: str, egress: str, image: str) -> None:
    """Raise ValueError unless `args` is exactly what build_proxy_args makes for these values."""
    try:
        tail = args[args.index(image) + 3 :]
    except ValueError as error:
        raise ValueError("expected proxy image not found") from error
    if len(tail) % 2 or any(flag != "--allow" for flag in tail[::2]):
        raise ValueError(f"unexpected proxy arguments: {tail}")
    if args != build_proxy_args(name, egress, image, tail[1::2]):
        raise ValueError("proxy arguments differ from the hardened form")


def _denied(log: bytes) -> list[tuple[str, int]]:
    denied = []
    for line in log.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("event") != "denied":
            continue
        host, port = event.get("host"), event.get("port")
        if isinstance(host, str) and type(port) is int:
            denied.append((host, port))
    return denied


def download_dependencies(
    spec: RunSpec,
    limits: SandboxLimits,
    ops: DockerOps,
    allowed_roots: Sequence[Path],
    pristine: Path,
    proxy_image: str,
    timeout_s: float,
) -> DownloadRecord:
    if not 0 < timeout_s <= MAX_DOWNLOAD_S:
        raise ValueError(f"timeout must be in (0, {MAX_DOWNLOAD_S}] seconds, got {timeout_s}")
    workspace, source = spec.workspace.resolve(), pristine.resolve()
    if workspace.is_relative_to(source) or source.is_relative_to(workspace):
        raise ValueError(f"workspace and pristine checkout overlap: {workspace} and {source}")
    allowlist = project_allowlist(pristine)
    hosts = sorted(allowlist.hosts)
    run_id = secrets.token_hex(8)
    internal, egress, proxy = (f"rootflake-{kind}-{run_id}" for kind in ("dl", "eg", "px"))
    created: list[str] = []
    proxy_created = False
    fetch_started = False
    fetch_name = f"rootflake-{run_id}"
    log_truncated = False
    outcome, exit_code, output = DownloadOutcome.INFRASTRUCTURE, None, ""
    denied: list[tuple[str, int]] = []
    cleanup_errors: list[str] = []
    try:
        for network, internal_only in ((internal, True), (egress, False)):
            create = ["docker", "network", "create", "--driver", "bridge", "--ipv6=false"]
            if internal_only:
                create.append("--internal")
            if ops.docker([*create, network]).exit_code != 0:
                raise RuntimeError(f"could not create network {network}")
            created.append(network)
        proxy_args = build_proxy_args(proxy, egress, proxy_image, hosts)
        check_proxy_args(proxy_args, proxy, egress, proxy_image)
        proxy_created = True
        if ops.start_proxy(proxy_args, proxy, egress, proxy_image).exit_code != 0:
            raise RuntimeError("could not start the proxy")
        if ops.docker(["docker", "network", "connect", internal, proxy]).exit_code != 0:
            raise RuntimeError("could not attach the proxy to the internal network")
        members = ops.docker(
            ["docker", "network", "inspect", internal, "--format",
             "{{range .Containers}}{{println .Name}}{{end}}"]
        )  # fmt: skip
        if members.exit_code != 0 or members.stdout.decode().split() != [proxy]:
            raise RuntimeError(f"unexpected members on {internal}")
        download = Download(network=internal, proxy=f"{proxy}:{PROXY_PORT}")
        attempt = replace(spec, command=[FETCH_COMMAND], name=fetch_name)
        args = build_download_args(attempt, limits, download)
        check_args(args, allowed_roots, spec.image, download=download)
        fetch_started = True
        result = ops.run(args, timeout_s)
        if result.timed_out:
            ops.stop(attempt.name)
            outcome = DownloadOutcome.TIMEOUT
        else:
            outcome = DownloadOutcome.OK if result.exit_code == 0 else DownloadOutcome.FAILED
        exit_code = result.exit_code
        output = result_output(result)
    except RuntimeError as error:
        output = str(error)
    finally:
        # Each step is guarded on its own: a failure in one must never leave a container or a
        # network behind.
        if fetch_started:
            try:
                if ops.docker(["docker", "rm", "-f", fetch_name]).exit_code != 0:
                    cleanup_errors.append(f"download container {fetch_name} not removed")
            except Exception as error:
                cleanup_errors.append(f"download container {fetch_name} not removed: {error}")
        if proxy_created:
            try:
                logs = ops.docker(["docker", "logs", proxy])
                log_truncated = bool(logs.stdout_dropped or logs.stderr_dropped)
                denied = _denied(logs.stderr + logs.stdout)
            except Exception as error:
                cleanup_errors.append(f"proxy {proxy} logs not read: {error}")
            try:
                if ops.docker(["docker", "rm", "-f", proxy]).exit_code != 0:
                    cleanup_errors.append(f"proxy {proxy} not removed")
            except Exception as error:
                cleanup_errors.append(f"proxy {proxy} not removed: {error}")
        for network in created:
            try:
                if ops.docker(["docker", "network", "rm", network]).exit_code != 0:
                    cleanup_errors.append(f"network {network} not removed")
            except Exception as error:
                cleanup_errors.append(f"network {network} not removed: {error}")
    return DownloadRecord(
        outcome=outcome,
        allowlist=set(hosts),
        rejected=allowlist.rejected,
        denied=denied,
        exit_code=exit_code,
        output=output,
        cleanup_errors=cleanup_errors,
        proxy_log_truncated=log_truncated,
    )
