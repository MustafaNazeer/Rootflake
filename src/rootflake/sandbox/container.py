"""Builds and checks the `docker run` arguments for every container that runs target code.

The flag set follows docs/adr/0005-local-sandbox-hardening.md and the sandbox hardening
checklist. `check_args` is the last gate before a container starts. It is a strict
allowlist: every flag must appear as its own token, in the exact count expected, with a
value that matches an exact pattern. Docker's own parsers accept more forms than this
(inline `--flag=value`, CSV quoting and key aliases in `--mount`), and every extra form is
a way around the gate, so none of them is accepted.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

SANDBOX_USER = "10001:10001"
RUNTIME = "runsc"
SUPPORTED_JDKS = (8, 11, 17)
WORKSPACE = "/workspace"
REPO_TAIL = "/m2/tail"
IMAGE_REPO_TAIL = "/opt/m2-tail"
REPO_HEAD = "/tmp/m2"
MAVEN_HEAP = "-Xmx768m"
HOME = "/home/sandbox"

COMPUTED_ENV = frozenset(
    {
        "HOME",
        "ROOTFLAKE_JDK",
        "ROOTFLAKE_MAX_PROCS",
        "MAVEN_OPTS",
        "MAVEN_ARGS",
        "JAVA_TOOL_OPTIONS",
    }
)
ALLOWED_ENV = COMPUTED_ENV | {"TZ"}

FORBIDDEN_FLAGS = frozenset(
    {
        "--privileged",
        "--cap-add",
        "--device",
        "--pid",
        "--ipc",
        "--userns",
        "--uts",
        "--volumes-from",
        "--volume",
        "-v",
        "--publish",
        "-p",
        "--add-host",
        "--group-add",
        "--cgroup-parent",
        "--cgroupns",
    }
)

DIGEST_RE = re.compile(r"[a-z0-9][a-z0-9._/-]*(:[A-Za-z0-9._-]+)?@sha256:[0-9a-f]{64}")
ZONE_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*")
SIZE_RE = re.compile(r"([1-9][0-9]{0,5})([kmg])")
SAFE_PATH_RE = re.compile(r"/[A-Za-z0-9._/-]*")
ENV_VALUE_RE = re.compile(r"[ -~]*")
NAME_RE = re.compile(r"rootflake-[a-z0-9]{8,40}")

MEMORY_MIB = (256, 16384)
CPUS = (1, 8)
PIDS = (64, 4096)
NOFILE = (256, 65536)
STOP_TIMEOUT_S = (1, 60)
LOG_MAX_MIB = (1, 100)


def _in_range(name: str, value: int, bounds: tuple[int, int]) -> None:
    low, high = bounds
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low} and {high}, got {value}")


def _size(name: str, value: str) -> str:
    if not SIZE_RE.fullmatch(value):
        raise ValueError(f"{name} must look like 128m or 1g, got {value!r}")
    return value


@dataclass(frozen=True)
class SandboxLimits:
    memory_mib: int = 4096
    cpus: int = 2
    pids: int = 1024
    nofile: int = 4096
    tmp_size: str = "1g"
    home_size: str = "128m"
    stop_timeout_s: int = 10
    log_max_mib: int = 10

    def __post_init__(self) -> None:
        _in_range("memory_mib", self.memory_mib, MEMORY_MIB)
        _in_range("cpus", self.cpus, CPUS)
        _in_range("pids", self.pids, PIDS)
        _in_range("nofile", self.nofile, NOFILE)
        _in_range("stop_timeout_s", self.stop_timeout_s, STOP_TIMEOUT_S)
        _in_range("log_max_mib", self.log_max_mib, LOG_MAX_MIB)
        _size("tmp_size", self.tmp_size)
        _size("home_size", self.home_size)


@dataclass(frozen=True)
class RunSpec:
    image: str
    workspace: Path
    maven_repo: Path
    jdk: int
    command: list[str]
    name: str
    timezone: str | None = None


def _safe_path(name: str, path: Path) -> str:
    text = str(path)
    if not SAFE_PATH_RE.fullmatch(text) or ".." in path.parts:
        raise ValueError(f"{name} must be an absolute path of plain characters: {text!r}")
    return text


def build_docker_args(spec: RunSpec, limits: SandboxLimits) -> list[str]:
    if not DIGEST_RE.fullmatch(spec.image):
        raise ValueError(f"image must be pinned by digest: {spec.image!r}")
    if type(spec.jdk) is not int or spec.jdk not in SUPPORTED_JDKS:
        raise ValueError(f"unsupported JDK {spec.jdk}; supported: {SUPPORTED_JDKS}")
    if not spec.command:
        raise ValueError("command must not be empty")
    if not NAME_RE.fullmatch(spec.name):
        raise ValueError(f"container name must match {NAME_RE.pattern}: {spec.name!r}")
    workspace = _safe_path("workspace", spec.workspace)
    repo = _safe_path("maven_repo", spec.maven_repo)
    memory = f"{limits.memory_mib}m"
    args = [
        "docker", "run", "--rm",
        "--name", spec.name,
        "--runtime", RUNTIME,
        "--network", "none",
        "--user", SANDBOX_USER,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges=true",
        "--read-only",
        "--tmpfs", f"/tmp:rw,exec,nosuid,nodev,size={limits.tmp_size}",
        "--tmpfs", f"/home/sandbox:rw,nosuid,nodev,size={limits.home_size}",
        "--mount", f"type=bind,src={workspace},dst={WORKSPACE}",
        "--mount", f"type=bind,src={repo},dst={REPO_TAIL},readonly",
        "--workdir", WORKSPACE,
        "--memory", memory,
        "--memory-swap", memory,
        "--cpus", str(limits.cpus),
        "--pids-limit", str(limits.pids),
        "--ulimit", f"nofile={limits.nofile}:{limits.nofile}",
        "--init",
        "--stop-timeout", str(limits.stop_timeout_s),
        "--log-opt", f"max-size={limits.log_max_mib}m",
        "--log-opt", "max-file=1",
    ]  # fmt: skip
    for key, value in _env(spec, limits).items():
        args += ["--env", f"{key}={value}"]
    return args + [spec.image, *spec.command]


MAVEN_ARGS = " ".join(
    [
        "--batch-mode",
        "--offline",
        f"-Dmaven.repo.local={REPO_HEAD}",
        f"-Dmaven.repo.local.tail={REPO_TAIL},{IMAGE_REPO_TAIL}",
    ]
)


def _tool_options(memory_mib: str, cpus: str, timezone: str | None) -> str:
    # JDK 8 ignores cgroup v2 limits on hosts where it finds controllers missing, so every
    # JVM is told the container's memory and CPU count instead of trusting ergonomics.
    options = [f"-XX:MaxRAM={memory_mib}m", f"-XX:ActiveProcessorCount={cpus}"]
    if timezone is not None:
        options.append(f"-Duser.timezone={timezone}")
    return " ".join(options)


def _check_zone(timezone: str) -> None:
    if len(timezone) > 64 or not ZONE_RE.fullmatch(timezone):
        raise ValueError(f"malformed time zone: {timezone!r}")


def _env(spec: RunSpec, limits: SandboxLimits) -> dict[str, str]:
    env = {
        "HOME": HOME,
        "ROOTFLAKE_JDK": str(spec.jdk),
        "ROOTFLAKE_MAX_PROCS": str(limits.pids),
        "MAVEN_OPTS": MAVEN_HEAP,
        "MAVEN_ARGS": MAVEN_ARGS,
    }
    if spec.timezone is not None:
        _check_zone(spec.timezone)
        env["TZ"] = spec.timezone
    env["JAVA_TOOL_OPTIONS"] = _tool_options(
        str(limits.memory_mib), str(limits.cpus), spec.timezone
    )
    return env


def _int_in(flag: str, value: str, bounds: tuple[int, int]) -> int:
    if not re.fullmatch(r"[0-9]{1,6}", value):
        raise ValueError(f"{flag} must be a plain positive integer, got {value!r}")
    number = int(value)
    _in_range(flag, number, bounds)
    return number


def _exact(expected: str):
    def check(flag: str, value: str) -> None:
        if value != expected:
            raise ValueError(f"{flag} must be {expected!r}, got {value!r}")

    return check


def _name(flag: str, value: str) -> None:
    if not NAME_RE.fullmatch(value):
        raise ValueError(f"{flag} must match {NAME_RE.pattern}, got {value!r}")


def _memory(flag: str, value: str) -> None:
    match = re.fullmatch(r"([0-9]{1,6})m", value)
    if not match:
        raise ValueError(f"{flag} must look like 4096m, got {value!r}")
    _in_range(flag, int(match.group(1)), MEMORY_MIB)


def _ulimit(flag: str, value: str) -> None:
    match = re.fullmatch(r"nofile=([0-9]{1,6}):([0-9]{1,6})", value)
    if not match or match.group(1) != match.group(2):
        raise ValueError(f"{flag} must be nofile=N:N, got {value!r}")
    _in_range(flag, int(match.group(1)), NOFILE)


SINGLE_VALUE_FLAGS = {
    "--name": _name,
    "--runtime": _exact(RUNTIME),
    "--network": _exact("none"),
    "--user": _exact(SANDBOX_USER),
    "--cap-drop": _exact("ALL"),
    "--security-opt": _exact("no-new-privileges=true"),
    "--workdir": _exact(WORKSPACE),
    "--memory": _memory,
    "--memory-swap": _memory,
    "--cpus": lambda f, v: _int_in(f, v, CPUS),
    "--pids-limit": lambda f, v: _int_in(f, v, PIDS),
    "--ulimit": _ulimit,
    "--stop-timeout": lambda f, v: _int_in(f, v, STOP_TIMEOUT_S),
}
MULTI_VALUE_FLAGS = ("--tmpfs", "--mount", "--log-opt", "--env")
BOOLEAN_FLAGS = ("--rm", "--read-only", "--init")


def check_args(args: list[str], allowed_roots: Sequence[Path], expected_image: str) -> None:
    """Raise ValueError unless `args` is exactly the hardened command the builder makes.

    Mount sources must exist, contain no symlinks, and sit strictly inside a managed root.
    """
    if args[:2] != ["docker", "run"]:
        raise ValueError("expected a docker run command")
    seen: dict[str, list[str]] = {}
    i = 2
    while i < len(args) and args[i].startswith("-"):
        token = args[i]
        name = token.split("=", 1)[0]
        if name in FORBIDDEN_FLAGS:
            raise ValueError(f"forbidden flag: {name}")
        if "=" in token:
            raise ValueError(f"flags must not carry inline values: {token!r}")
        if token in BOOLEAN_FLAGS:
            seen.setdefault(token, []).append("")
            i += 1
            continue
        if token not in SINGLE_VALUE_FLAGS and token not in MULTI_VALUE_FLAGS:
            raise ValueError(f"flag not allowed: {token}")
        if i + 1 >= len(args) or not args[i + 1] or args[i + 1].startswith("-"):
            raise ValueError(f"missing or suspicious value for {token}")
        seen.setdefault(token, []).append(args[i + 1])
        i += 2

    if i >= len(args) or not DIGEST_RE.fullmatch(args[i]):
        raise ValueError(f"image must be pinned by digest: {args[i] if i < len(args) else None!r}")
    if args[i] != expected_image:
        raise ValueError(f"image {args[i]!r} is not the expected image {expected_image!r}")
    command = args[i + 1 :]
    if not command or any(not part or "\0" in part for part in command):
        raise ValueError("command must be non empty with no empty or NUL parts")

    for flag in BOOLEAN_FLAGS:
        if seen.get(flag) != [""]:  # exactly once
            raise ValueError(f"{flag} must appear exactly once")
    for flag, check in SINGLE_VALUE_FLAGS.items():
        values = seen.get(flag, [])
        if len(values) != 1:
            raise ValueError(f"{flag} must appear exactly once, got {len(values)}")
        check(flag, values[0])
    if seen["--memory"] != seen["--memory-swap"]:
        raise ValueError("--memory-swap must equal --memory so swap is unavailable")
    _check_tmpfs(seen.get("--tmpfs", []))
    _check_log_opts(seen.get("--log-opt", []))
    _check_mounts(seen.get("--mount", []), allowed_roots)
    _check_env(seen.get("--env", []), seen)


def _check_tmpfs(values: list[str]) -> None:
    patterns = {
        "/tmp": re.compile(r"/tmp:rw,exec,nosuid,nodev,size=([1-9][0-9]{0,5}[kmg])"),
        "/home/sandbox": re.compile(r"/home/sandbox:rw,nosuid,nodev,size=([1-9][0-9]{0,5}[kmg])"),
    }
    targets = []
    for value in values:
        target = value.split(":", 1)[0]
        pattern = patterns.get(target)
        if pattern is None or not pattern.fullmatch(value):
            raise ValueError(f"tmpfs not allowed: {value!r}")
        targets.append(target)
    if sorted(targets) != sorted(patterns):
        raise ValueError(f"expected exactly the /tmp and home tmpfs mounts, got {targets}")


def _check_log_opts(values: list[str]) -> None:
    sizes = [v for v in values if v.startswith("max-size=")]
    if sorted(values) != sorted(sizes + ["max-file=1"]) or len(sizes) != 1:
        raise ValueError(f"log options must be one max-size and max-file=1, got {values}")
    match = re.fullmatch(r"max-size=([0-9]{1,3})m", sizes[0])
    if not match:
        raise ValueError(f"bad log max-size: {sizes[0]!r}")
    _in_range("log max-size", int(match.group(1)), LOG_MAX_MIB)


MOUNT_RES = {
    WORKSPACE: re.compile(r"type=bind,src=(/[A-Za-z0-9._/-]*),dst=/workspace"),
    REPO_TAIL: re.compile(r"type=bind,src=(/[A-Za-z0-9._/-]*),dst=/m2/tail,readonly"),
}


def _resolved_roots(allowed_roots: Sequence[Path]) -> list[Path]:
    if not allowed_roots:
        raise ValueError("at least one managed root is required")
    roots = []
    for root in allowed_roots:
        if not root.is_absolute() or root == Path("/"):
            raise ValueError(f"managed root must be absolute and not /: {root}")
        roots.append(root.resolve(strict=True))
    return roots


def _check_mounts(values: list[str], allowed_roots: Sequence[Path]) -> None:
    roots = _resolved_roots(allowed_roots)
    targets = []
    for value in values:
        matches = [(t, m) for t, p in MOUNT_RES.items() if (m := p.fullmatch(value))]
        if not matches:
            raise ValueError(f"mount not allowed: {value!r}")
        target, match = matches[0]
        source = Path(match.group(1))
        if ".." in source.parts:
            raise ValueError(f"mount source contains '..': {value!r}")
        try:
            real = source.resolve(strict=True)
        except OSError as error:
            raise ValueError(f"mount source does not exist: {value!r}") from error
        if real != source:
            raise ValueError(f"mount source goes through a symlink: {value!r}")
        if not any(real != root and real.is_relative_to(root) for root in roots):
            raise ValueError(f"mount source is outside the managed roots: {value!r}")
        targets.append(target)
    if sorted(targets) != sorted(MOUNT_RES):
        raise ValueError(f"expected exactly the workspace and repository mounts, got {targets}")


def _check_env(values: list[str], seen: dict[str, list[str]]) -> None:
    env: dict[str, str] = {}
    for entry in values:
        key, sep, value = entry.partition("=")
        if not sep:
            raise ValueError(f"--env needs an explicit value, not a host copy: {entry!r}")
        if key not in ALLOWED_ENV:
            raise ValueError(f"environment variable not allowed in the sandbox: {key}")
        if key in env:
            raise ValueError(f"duplicate environment variable: {key}")
        if not ENV_VALUE_RE.fullmatch(value):
            raise ValueError(f"environment value has control characters: {key}")
        env[key] = value
    missing = sorted(COMPUTED_ENV - env.keys())
    if missing:
        raise ValueError(f"missing runner environment: {missing}")
    timezone = env.get("TZ")
    if timezone is not None:
        _check_zone(timezone)
    memory_mib = seen["--memory"][0].removesuffix("m")
    expected = {
        "HOME": HOME,
        "ROOTFLAKE_MAX_PROCS": seen["--pids-limit"][0],
        "MAVEN_OPTS": MAVEN_HEAP,
        "MAVEN_ARGS": MAVEN_ARGS,
        "JAVA_TOOL_OPTIONS": _tool_options(memory_mib, seen["--cpus"][0], timezone),
    }
    for key, value in expected.items():
        if env[key] != value:
            raise ValueError(f"{key} must be {value!r}, got {env[key]!r}")
    if env["ROOTFLAKE_JDK"] not in {str(v) for v in SUPPORTED_JDKS}:
        raise ValueError(f"ROOTFLAKE_JDK must be one of {SUPPORTED_JDKS}")
